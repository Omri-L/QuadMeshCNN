import torch
import torch.nn as nn
from threading import Thread
from models.layers.mesh_union import MeshUnion
import numpy as np
from heapq import heappop, heapify
from .mesh_rotation_utils import *


class MeshPool(nn.Module):

    def __init__(self, target, multi_thread=False):
        super(MeshPool, self).__init__()
        self.__out_target = target
        self.__multi_thread = multi_thread
        self.__fe = None
        self.__updated_fe = []
        self.__meshes = None
        self.__merge_edges = [-1, -1]

    def __call__(self, fe, meshes):
        return self.forward(fe, meshes)

    def forward(self, fe, meshes):
        self.__updated_fe = [[] for _ in range(len(meshes))]
        pool_threads = []
        self.__fe = fe
        self.__meshes = meshes
        # iterate over batch
        for mesh_index in range(len(meshes)):
            if self.__multi_thread:
                pool_threads.append(
                    Thread(target=self.__pool_main, args=(mesh_index,)))
                pool_threads[-1].start()
            else:
                self.__pool_main(mesh_index)
        if self.__multi_thread:
            for mesh_index in range(len(meshes)):
                pool_threads[mesh_index].join()
        a = self.__updated_fe
        out_features = torch.cat(self.__updated_fe).view(len(meshes), -1,
                                                         self.__out_target)
        return out_features

    def __pool_main(self, mesh_index):
        mesh = self.__meshes[mesh_index]
        queue = self.__build_queue(self.__fe[mesh_index, :, :mesh.edges_count],
                                   mesh.edges_count)
        # print('pooling target - %d, mesh filename: %s' % (self.__out_target, mesh.filename))
        last_count = mesh.edges_count + 1
        mask = np.ones(mesh.edges_count, dtype=np.bool)
        edge_groups = MeshUnion(mesh.edges_count, self.__fe.device)

        while mesh.edges_count > self.__out_target:
            value, edge_id = heappop(queue)
            edge_id = int(edge_id)
            # print('pool edge_id %d' % edge_id)
            if mask[edge_id]:
                status = self.__pool_edge(mesh, edge_id, mask, edge_groups)
        mesh.clean(mask, edge_groups)
        fe = edge_groups.rebuild_features(self.__fe[mesh_index], mask,
                                          self.__out_target)
        self.__updated_fe[mesh_index] = fe
        # print('finish pooling')

    def __pool_edge(self, mesh, edge_id, mask, edge_groups):
        """
        This function implements edge pooling algorithm:
        1. Clean mesh configuration from doublet edges and singlet edges.
        2. For a non-boundary edge check if:
            2.1. First edge side is "clean".
            2.2. Second edge side is "clean".
            2.3. edge one-ring neighborhood is.
        3. Run edge collapse algorithm.
        Args:
            mesh (Mesh): mesh structure input (will be updated during the
                         process).
            edge_id (int): edge identification number in the mesh.
            mask: (ndarray): array of boolean values which indicates if an edge
                             aleady been removed.
            edge_groups (MeshUnion): mesh union structure of edge groups in-
                                     order to keep track of edge features
                                     combinations.

        Returns:
            status (bool) - True if pool_edge algorithm succeeded,
                            False otherwise.

        """
        # 1. Pool mesh operations
        self.pool_mesh_operations(mesh, mask, edge_groups)

        # Check if edge_id have boundaries
        if self.has_boundaries(mesh, edge_id):
            return False

        # 2. Check edge configuration validity
        if self.__clean_side(mesh, edge_id, 0) \
                and self.__clean_side(mesh, edge_id, 3) \
                and self.__is_one_ring_valid(mesh, edge_id):

            # 3. Edge collapse algorithm
            status = self.edge_collapse(edge_id, mesh, mask, edge_groups)
            self.pool_mesh_operations(mesh, mask, edge_groups)
            return status
        else:
            return False

    def edge_collapse(self, edge_id, mesh, mask, edge_groups):
        """
        This function implements edge collapse algorithm inspired by the paper:
        "Practical quad mesh simplification" Tarini et al.
        The algorithm goes as follows:
        1. Extract edge mesh information (for each vertex extract edge
           connections and their vertices).
        2. Check if the edges connected to u and v have boundaries.
        3. Rotate the edges connected to u and re-build their neighborhood.
        4. Perform diagonal collapse from v to u - collapse the two edges from
           the original edge_id neighborhood which are connected to v and
           reconnect all the other edges connected to v with u. Re-build all
           edges neighborhood.
        5. Union edges groups according to new feature edges combinations.
        """
        # 1. Get edge info
        u, v_e_u, e_u, v, v_e_v, e_v = get_edge_hood_info(mesh, edge_id)

        # 2. Check if u and v edges are with boundaries
        correct_config, u, v_e_u, e_u, v, v_e_v, e_v = \
            check_u_v_boundaries(mesh, u, v_e_u, e_u, v, v_e_v, e_v)

        if not correct_config:
            return False

        # 3. Edges rotations around vertex u
        mesh, new_features_combination_dict, diag_vertices = \
            edge_rotations(u, e_u, v_e_u, mesh)

        if diag_vertices is None:
            return False

        # 3. collapse another 2 edges connected to the other vertex v and
        # reconnect other edges from v connection to u connection
        e_v = mesh.ve[v].copy()  # edges connected to vertex v
        self.collapse_other_vertex_v(mesh, u, v, e_v, diag_vertices,
                                     new_features_combination_dict,
                                     edge_groups, mask)

        # 4. union edge groups
        MeshPool.__union_groups_at_once(mesh, edge_groups,
                                        new_features_combination_dict)
        return True

    def collapse_other_vertex_v(self, mesh, u, v, e_v, diag_vertices,
                                new_features_combination_dict, edge_groups,
                                mask):
        """
        This function implements the diagonal collapse from vertex v to vertex
        u according to the following steps:
        1. Check if vertex v is a doublet edges configuration.
           If it is - clear the doublet and return (no other collapse is
           needed).
        2. Collapse (and finally remove) the 2 edges connected to v in the
           original neighborhood of edge_id.
        3. Re-connect all the other edges connected to v with u
        4. Re-build all relevant edges neighborhoods.
        """
        if self.pool_doublets(mesh, mask, edge_groups, [v]):
            return

        old_mesh = deepcopy(mesh)

        e_to_collapse = []  # edges we should remove
        collapsed_e_to_orig_e_dict = dict()  # to which edge the collpased edge combined with
        e_to_reconnect_with_u = []  # edges we should re-connect with vertex u

        for e in e_v:
            u_e, v_e = mesh.edges[e, :]
            if u_e == v:  # make sure u_e is the other vertex
                u_e = v_e
                v_e = v
            # if it is an edge of the closet hood
            if u_e in diag_vertices or v_e in diag_vertices:
                e_to_collapse.append(e)

                for key in new_features_combination_dict.keys():
                    if u_e in mesh.edges[key]:
                        edge_to_add_feature = key
                        new_features_combination_dict[key].append(e)
                        collapsed_e_to_orig_e_dict[e] = key
                        break
                # collapse
                self.remove_edge(mesh, e, edge_groups, mask)

            else:
                e_to_reconnect_with_u.append(e)
                mesh.ve[v].remove(e)
                mesh.ve[u].append(e)
                if mesh.edges[e, 0] == v:
                    mesh.edges[e, 0] = u
                else:
                    mesh.edges[e, 1] = u

        # fix hood for edges which re-connected to u
        already_built_edges_hood = []
        for e in e_to_reconnect_with_u:
            hood = old_mesh.gemm_edges[e]
            edges_to_check = [e] + list(hood)
            for edge in edges_to_check:
                if edge in already_built_edges_hood or edge in e_to_collapse:
                    continue

                already_built_edges_hood.append(edge)
                old_hood = old_mesh.gemm_edges[edge]
                new_hood = mesh.gemm_edges[edge]
                # replace any e_to_collapse edge by the matched edge
                for e_collapse in e_to_collapse:
                    if np.any([h == e_collapse for h in old_hood]):
                        e_collapse_pos = \
                            np.where([h == e_collapse for h in old_hood])[0][0]
                        new_hood[e_collapse_pos] = collapsed_e_to_orig_e_dict[
                            e_collapse]

        # now fix hood for the rotated edges
        for key in collapsed_e_to_orig_e_dict:
            edge = collapsed_e_to_orig_e_dict[key]
            old_hood = old_mesh.gemm_edges[edge]
            new_hood = mesh.gemm_edges[edge]
            already_built_edges_hood.append(edge)
            if key in old_hood[0:3]:
                if edge not in old_mesh.gemm_edges[key, 0:3]:
                    new_hood[0:3] = old_mesh.gemm_edges[key, 0:3]
                else:
                    new_hood[0:3] = old_mesh.gemm_edges[key, 3:6]
            elif key in old_hood[3:6]:
                if edge not in old_mesh.gemm_edges[key, 0:3]:
                    new_hood[3:6] = old_mesh.gemm_edges[key, 0:3]
                else:
                    new_hood[3:6] = old_mesh.gemm_edges[key, 3:6]
            else:
                assert (False)

            for i, e in enumerate(new_hood):
                if e in collapsed_e_to_orig_e_dict.keys():
                    new_hood[i] = collapsed_e_to_orig_e_dict[e]

        # fix hood order:
        fix_mesh_hood_order(mesh, already_built_edges_hood)

        # fix sides
        fix_mesh_sides(mesh, already_built_edges_hood)

        # merge vertex v with vertex u
        mesh.merge_vertices(u, v)
        return

    def pool_mesh_operations(self, mesh, mask, edge_groups):
        """
        This function implements the mesh cleaning process. In-order to keep
        mesh with valid connectivity and without edge neighborhoods ambiguities
        we keep mesh clear from "doublet" and "singlet" (TBD) edges.
        """
        # clear doublets and build new hood
        doublet_cleared = self.pool_doublets(mesh, mask, edge_groups)
        while doublet_cleared:
            doublet_cleared = self.pool_doublets(mesh, mask, edge_groups)

        # TBD
        # clear singlets and build new hood
        # clear_singlets(mesh, mask, edge_groups)
        return

    def pool_doublets(self, mesh, mask, edge_groups, vertices=None):
        """
        This function finds doublet configuration and removes it from the mesh.
        Args:
            mesh (Mesh): mesh structure
            mask (ndarray): array of boolean which indicates which edge removed
            edge_groups (MeshUnion): mesh union strcture contain all edges
                                     groups of edge features combinations.
            vertices (list, optional): if not None, check only this list of
                                       vertices in the mesh.
                                       Otherwise - check all mesh.

        Returns:
            boolean - True if doublet found and removed.
                      False - otherwise.
        """
        doublet_vertices, doublet_pairs_edges = find_doublets(mesh, vertices)
        if len(doublet_vertices) == 0:
            return False

        for pair in doublet_pairs_edges:
            doubelt_to_replaced_edge, doubelt_to_replaced_edge_other_side = \
                clear_doublet_pair(mesh, mask, pair)

            # union groups for features
            for key in doubelt_to_replaced_edge.keys():
                MeshPool.__union_groups(mesh, edge_groups, key,
                                        doubelt_to_replaced_edge[key])

            # union groups for features
            for key in doubelt_to_replaced_edge_other_side.keys():
                MeshPool.__union_groups(mesh, edge_groups, key,
                                        doubelt_to_replaced_edge_other_side[
                                            key])

            for e in pair:
                MeshPool.__remove_group(mesh, edge_groups, e)

        return True

    @staticmethod
    def remove_edge(mesh, e, edge_groups, mask):
        """
        Removes an edge:
        Remove it from edge groups (MeshUnion structure)
        Indicate it in the "mask" array
        Remove it from the mesh structure.
        """
        MeshPool.__remove_group(mesh, edge_groups, e)
        mask[e] = False
        mesh.remove_edge(e)
        mesh.edges[e] = [-1, -1]
        mesh.edges_count -= 1
        mesh.gemm_edges[e] = [-1, -1, -1, -1, -1, -1]

    def __clean_side(self, mesh, edge_id, side):
        """
        Checks how many shared items have each pair neighborhood edge of
        edge_id (specific side) in their neighborhood.
        """
        if mesh.edges_count <= self.__out_target:
            return False
        info = MeshPool.__get_face_info(mesh, edge_id, side)
        key_a, key_b, key_c, side_a, side_b, side_c, \
        other_side_a, other_side_b, other_side_c, \
        other_keys_a, other_keys_b, other_keys_c = info
        shared_items_ab = MeshPool.__get_shared_items(other_keys_a,
                                                      other_keys_b)
        shared_items_ac = MeshPool.__get_shared_items(other_keys_a,
                                                      other_keys_c)
        shared_items_bc = MeshPool.__get_shared_items(other_keys_b,
                                                      other_keys_c)
        if len(shared_items_ab) <= 2 and len(shared_items_ac) <= 2 and \
                len(shared_items_bc) <= 2:
            return True
        else:
            assert (
                False)  # TODO: we shouldn't get here.
            return False

    @staticmethod
    def has_boundaries(mesh, edge_id):
        for edge in mesh.gemm_edges[edge_id]:
            if edge == -1 or -1 in mesh.gemm_edges[edge]:
                return True
        return False

    @staticmethod
    def __get_v_n(mesh, edge_id):
        return set(mesh.edges[mesh.ve[mesh.edges[edge_id, 0]]].reshape(-1)), \
               set(mesh.edges[mesh.ve[mesh.edges[edge_id, 1]]].reshape(-1)),

    def __is_one_ring_valid(self, mesh, edge_id):
        """
        Checks edge_id one-ring edges neighborhood is valid, i.e. only 4
        vertices can be shared from each side of the edge_id.
        """
        e_a = mesh.ve[mesh.edges[edge_id, 0]]
        e_b = mesh.ve[mesh.edges[edge_id, 1]]

        v_a = set()  # set of all neighbor + diagonal vertices of first edge vertex
        v_b = set()  # set of all neighbor + diagonal vertices of second edge vertex
        for e in e_a:
            if not e == edge_id:
                v_aa, v_ab = self.__get_v_n(mesh, e)
                v_a = set.union(set.union(v_aa, v_ab), v_a)

        for e in e_b:
            if not e == edge_id:
                v_ba, v_bb = self.__get_v_n(mesh, e)
                v_b = set.union(set.union(v_ba, v_bb), v_b)

        shared = v_a & v_b - set(mesh.edges[edge_id])
        return len(shared) == 4

    @staticmethod
    def __get_shared_items(list_a, list_b):
        shared_items = []
        for i in range(len(list_a)):
            for j in range(len(list_b)):
                if list_a[i] == list_b[j]:
                    shared_items.extend([i, j])
        return shared_items

    @staticmethod
    def __get_face_info(mesh, edge_id, side):
        key_a = mesh.gemm_edges[edge_id, side]
        key_b = mesh.gemm_edges[edge_id, side + 1]
        key_c = mesh.gemm_edges[edge_id, side + 2]
        side_a = mesh.sides[edge_id, side]
        side_b = mesh.sides[edge_id, side + 1]
        side_c = mesh.sides[edge_id, side + 2]
        other_side_a = (side_a - (side_a % 3) + 3) % 6
        other_side_b = (side_b - (side_b % 3) + 3) % 6
        other_side_c = (side_c - (side_c % 3) + 3) % 6
        other_keys_a = [mesh.gemm_edges[key_a, other_side_a],
                        mesh.gemm_edges[key_a, other_side_a + 1],
                        mesh.gemm_edges[key_a, other_side_a + 2]]
        other_keys_b = [mesh.gemm_edges[key_b, other_side_b],
                        mesh.gemm_edges[key_b, other_side_b + 1],
                        mesh.gemm_edges[key_b, other_side_b + 2]]
        other_keys_c = [mesh.gemm_edges[key_c, other_side_c],
                        mesh.gemm_edges[key_c, other_side_c + 1],
                        mesh.gemm_edges[key_c, other_side_c + 2]]
        return key_a, key_b, key_c, side_a, side_b, side_c, \
               other_side_a, other_side_b, other_side_c, \
               other_keys_a, other_keys_b, other_keys_c

    @staticmethod
    def __build_queue(features, edges_count):
        # delete edges with smallest norm
        squared_magnitude = torch.sum(features * features, 0)
        if squared_magnitude.shape[-1] != 1:
            squared_magnitude = squared_magnitude.unsqueeze(-1)
        edge_ids = torch.arange(edges_count, device=squared_magnitude.device,
                                dtype=torch.float32).unsqueeze(-1)
        heap = torch.cat((squared_magnitude, edge_ids), dim=-1).tolist()
        heapify(heap)
        return heap

    @staticmethod
    def __union_groups(mesh, edge_groups, source, target):
        edge_groups.union(source, target)
        mesh.union_groups(source, target)

    @staticmethod
    def __union_groups_at_once(mesh, edge_groups, targets_to_sources_dict):
        edge_groups.union_groups(targets_to_sources_dict)
        for target in targets_to_sources_dict.keys():
            for source in targets_to_sources_dict[target]:
                if target is not source:
                    mesh.union_groups(source, target)

    @staticmethod
    def __remove_group(mesh, edge_groups, index):
        edge_groups.remove_group(index)
        mesh.remove_group(index)
