import numpy as np
import os
import ntpath
from scipy.interpolate import interp2d
import random
from .mesh_rotation_utils import rotate_edges_around_vertex
import torchvision.transforms as transforms
from PIL import Image


def fill_mesh(mesh2fill, file: str, opt):
    load_path = get_mesh_path(file, opt.num_aug, prefix=mesh2fill.img_ind)
    if os.path.exists(load_path):
        mesh_data = np.load(load_path, encoding='latin1', allow_pickle=True)
    else:
        mesh_data = from_scratch(file, opt, mesh2fill.img_data)
        np.savez_compressed(load_path, gemm_edges=mesh_data.gemm_edges, vs=mesh_data.vs, edges=mesh_data.edges,
                            edges_count=mesh_data.edges_count, ve=mesh_data.ve, v_mask=mesh_data.v_mask,
                            filename=mesh_data.filename, sides=mesh_data.sides,
                            edge_lengths=mesh_data.edge_lengths, edge_areas=mesh_data.edge_areas,
                            features=mesh_data.features, img_data=mesh_data.img_data)
    mesh2fill.vs = mesh_data['vs']
    mesh2fill.edges = mesh_data['edges']
    mesh2fill.gemm_edges = mesh_data['gemm_edges']
    mesh2fill.edges_count = int(mesh_data['edges_count'])
    mesh2fill.ve = mesh_data['ve']
    mesh2fill.v_mask = mesh_data['v_mask']
    mesh2fill.filename = str(mesh_data['filename'])
    mesh2fill.edge_lengths = mesh_data['edge_lengths']
    mesh2fill.edge_areas = mesh_data['edge_areas']
    mesh2fill.features = mesh_data['features']
    mesh2fill.sides = mesh_data['sides']
    mesh2fill.img_data = mesh_data['img_data']


def get_mesh_path(file: str, num_aug: int, prefix: int):
    filename, _ = os.path.splitext(file)
    dir_name = os.path.dirname(filename)
    prefix = str(prefix)
    load_dir = os.path.join(dir_name, 'cache')
    load_file = os.path.join(load_dir, '%s_%03d.npz' % (prefix, np.random.randint(0, num_aug)))
    if not os.path.isdir(load_dir):
        os.makedirs(load_dir, exist_ok=True)
    return load_file


def from_scratch(file, opt, img_data):

    class MeshPrep:
        def __getitem__(self, item):
            return eval('self.' + item)

    mesh_data = MeshPrep()
    mesh_data.img_data = img_data
    mesh_data.vs = mesh_data.edges = None
    mesh_data.gemm_edges = mesh_data.sides = None
    mesh_data.edges_count = None
    mesh_data.ve = None
    mesh_data.v_mask = None
    mesh_data.filename = 'unknown'
    mesh_data.edge_lengths = None
    mesh_data.edge_areas = []
    mesh_data.vs, faces = fill_from_file(mesh_data, file)
    mesh_data.v_mask = np.ones(len(mesh_data.vs), dtype=bool)
    faces, face_areas = remove_non_manifolds(mesh_data, faces)
    if opt.num_aug > 1 and mesh_data.img_data is not None:
        augmentation(mesh_data, opt)
    build_gemm(mesh_data, faces, face_areas)
    if opt.num_aug > 1:
        post_augmentation(mesh_data, opt)
    mesh_data.features = extract_features(mesh_data)
    return mesh_data


def fill_from_file(mesh, file):
    mesh.filename = ntpath.split(file)[1]
    mesh.fullfilename = file
    vs, faces = [], []
    f = open(file)
    for line in f:
        line = line.strip()
        splitted_line = line.split()
        if not splitted_line:
            continue
        elif splitted_line[0] == 'v':
            vs.append([float(v) for v in splitted_line[1:4]])
        elif splitted_line[0] == 'f':
            face_vertex_ids = [int(c.split('/')[0]) for c in splitted_line[1:]]
            assert len(face_vertex_ids) == 4
            face_vertex_ids = [(ind - 1) if (ind >= 0) else (len(vs) + ind)
                               for ind in face_vertex_ids]
            faces.append(face_vertex_ids)
    f.close()
    vs = np.asarray(vs)
    faces = np.asarray(faces, dtype=int)
    assert np.logical_and(faces >= 0, faces < len(vs)).all()
    return vs, faces


def remove_non_manifolds(mesh, faces):
    mesh.ve = [[] for _ in mesh.vs]
    edges_set = set()
    mask = np.ones(len(faces), dtype=bool)
    _, face_areas = compute_face_normals_and_areas(mesh, faces)
    for face_id, face in enumerate(faces):
        if face_areas[face_id] == 0:
            mask[face_id] = False
            continue
        faces_edges = []
        is_manifold = False
        for i in range(4):
            cur_edge = (face[i], face[(i + 1) % 4])
            if cur_edge in edges_set:
                is_manifold = True
                break
            else:
                faces_edges.append(cur_edge)
        if is_manifold:
            mask[face_id] = False
        else:
            for idx, edge in enumerate(faces_edges):
                edges_set.add(edge)
    return faces[mask], face_areas[mask]


def build_gemm(mesh, faces, face_areas):
    """
    gemm_edges: array (#E x 4) of the 4 one-ring neighbors for each edge
    sides: array (#E x 4) indices (values of: 0,1,2,3) indicating where an edge is in the gemm_edge entry of the 4 neighboring edges
    for example edge i -> gemm_edges[gemm_edges[i], sides[i]] == [i, i, i, i]
    """
    mesh.ve = [[] for _ in mesh.vs]
    edge_nb = []
    sides = []
    edge2key = dict()
    edges = []
    edges_count = 0
    nb_count = []
    for face_id, face in enumerate(faces):
        faces_edges = []
        # print(face_id, face, edges_count)
        for i in range(4):
            cur_edge = (face[i], face[(i + 1) % 4])
            faces_edges.append(cur_edge)
        for idx, edge in enumerate(faces_edges):
            edge = tuple(sorted(list(edge)))
            faces_edges[idx] = edge
            if edge not in edge2key:
                edge2key[edge] = edges_count
                edges.append(list(edge))
                edge_nb.append([-1, -1, -1, -1, -1, -1])
                sides.append([-1, -1, -1, -1, -1, -1])
                mesh.ve[edge[0]].append(edges_count)
                mesh.ve[edge[1]].append(edges_count)
                mesh.edge_areas.append(0)
                nb_count.append(0)
                edges_count += 1
            mesh.edge_areas[edge2key[edge]] += face_areas[face_id] / 4
        for idx, edge in enumerate(faces_edges):
            edge_key = edge2key[edge]
            edge_nb[edge_key][nb_count[edge_key]] = edge2key[faces_edges[(idx + 1) % 4]]
            edge_nb[edge_key][nb_count[edge_key] + 1] = edge2key[faces_edges[(idx + 2) % 4]]
            edge_nb[edge_key][nb_count[edge_key] + 2] = edge2key[faces_edges[(idx + 3) % 4]]
            nb_count[edge_key] += 3
        for idx, edge in enumerate(faces_edges):
            edge_key = edge2key[edge]
            sides[edge_key][nb_count[edge_key] - 3] = nb_count[edge2key[faces_edges[(idx + 1) % 4]]] - 1
            sides[edge_key][nb_count[edge_key] - 2] = nb_count[edge2key[faces_edges[(idx + 2) % 4]]] - 2
            sides[edge_key][nb_count[edge_key] - 1] = nb_count[edge2key[faces_edges[(idx + 3) % 4]]] - 3
    mesh.edges = np.array(edges, dtype=np.int32)
    mesh.gemm_edges = np.array(edge_nb, dtype=np.int64)
    mesh.sides = np.array(sides, dtype=np.int64)
    mesh.edges_count = edges_count
    mesh.edge_areas = np.array(mesh.edge_areas, dtype=np.float32) / np.sum(face_areas) #todo whats the difference between edge_areas and edge_lenghts?


def compute_face_normals_and_areas(mesh, faces):
    face_normals = np.cross(mesh.vs[faces[:, 1]] - mesh.vs[faces[:, 0]],
                            mesh.vs[faces[:, 2]] - mesh.vs[faces[:, 1]])
    face_areas = np.sqrt((face_normals ** 2).sum(axis=1))
    face_normals /= face_areas[:, np.newaxis]
    assert (not np.any(face_areas[:, np.newaxis] == 0)), 'has zero area face: %s' % mesh.filename
    face_areas *= 0.5
    return face_normals, face_areas


# Data augmentation methods
def augmentation(mesh, opt):
    if hasattr(opt, 'scale_verts') and opt.scale_verts:
        scale_verts(mesh, opt.scale_verts)

    if hasattr(opt, 'hr_flip_img') and opt.hr_flip_img:
        hflip = transforms.RandomHorizontalFlip(opt.hr_flip_img)
        mesh.img_data = np.array(hflip(Image.fromarray(mesh.img_data)))

    if hasattr(opt, 'vr_flip_img') and opt.vr_flip_img:
        vflip = transforms.RandomVerticalFlip(opt.vr_flip_img)
        mesh.img_data = np.array(vflip(Image.fromarray(mesh.img_data)))

    return


def post_augmentation(mesh, opt):
    if hasattr(opt, 'rotate_edges') and opt.rotate_edges:
        num_rotations = int(mesh.edges_count * opt.rotate_edges)
        random.seed(0)
        edges = np.random.choice(mesh.edges_count, num_rotations)
        for edge in edges:
            # print('post_aug_edge {}'.format(edge))
            v = mesh.edges[edge]
            # rotate edge only if 4 edges connected to the vertices of it
            mesh = rotate_edges_around_vertex(mesh, edge)

        return


def slide_verts(mesh, prct):
    edge_points = get_edge_points(mesh)
    dihedral = dihedral_angle(mesh, edge_points).squeeze() #todo make fixed_division epsilon=0
    thr = np.mean(dihedral) + np.std(dihedral)
    vids = np.random.permutation(len(mesh.ve))
    target = int(prct * len(vids))
    shifted = 0
    for vi in vids:
        if shifted < target:
            edges = mesh.ve[vi]
            if min(dihedral[edges]) > 2.65:
                edge = mesh.edges[np.random.choice(edges)]
                vi_t = edge[1] if vi == edge[0] else edge[0]
                nv = mesh.vs[vi] + np.random.uniform(0.2, 0.5) * (mesh.vs[vi_t] - mesh.vs[vi])
                mesh.vs[vi] = nv
                shifted += 1
        else:
            break
    mesh.shifted = shifted / len(mesh.ve)


def scale_verts(mesh, p=0.1):
    num_verts_to_change = int(p * len(mesh.vs))
    random.seed(0)
    vs_ind_to_change = np.random.choice(len(mesh.vs), num_verts_to_change)
    eps = 1e-6

    vs = mesh.vs.copy()
    for dim in range(mesh.vs.shape[1]):
        min_val = min(vs[:, dim])
        max_val = max(vs[:, dim])
        for ind in vs_ind_to_change:
            # assumes uniform grid with step of 2  # TODO change this
            max_step_before = -2+eps
            max_step_after = 2-eps
            random.seed(0)
            new_grid = mesh.vs[ind, dim] + np.random.uniform(max_step_before,
                                                             max_step_after)
            new_grid = min(max(new_grid, min_val), max_val)
            vs[ind, dim] = new_grid

    mesh.vs = vs


def angles_from_faces(mesh, edge_faces, faces):
    normals = [None, None]
    for i in range(2):
        edge_a = mesh.vs[faces[edge_faces[:, i], 2]] - mesh.vs[faces[edge_faces[:, i], 1]]
        edge_b = mesh.vs[faces[edge_faces[:, i], 1]] - mesh.vs[faces[edge_faces[:, i], 0]]
        normals[i] = np.cross(edge_a, edge_b)
        div = fixed_division(np.linalg.norm(normals[i], ord=2, axis=1), epsilon=0)
        normals[i] /= div[:, np.newaxis]
    dot = np.sum(normals[0] * normals[1], axis=1).clip(-1, 1)
    angles = np.pi - np.arccos(dot)
    return angles


def check_area(mesh, faces):
    face_normals = np.cross(mesh.vs[faces[:, 1]] - mesh.vs[faces[:, 0]],
                            mesh.vs[faces[:, 2]] - mesh.vs[faces[:, 1]])
    face_areas = np.sqrt((face_normals ** 2).sum(axis=1))
    face_areas *= 0.5
    return face_areas[0] > 0 and face_areas[1] > 0


def set_edge_lengths(mesh, edge_points=None):
    if edge_points is not None:
        edge_points = get_edge_points(mesh)
    edge_lengths = np.linalg.norm(mesh.vs[edge_points[:, 0]] - mesh.vs[edge_points[:, 1]], ord=2, axis=1)
    mesh.edge_lengths = edge_lengths


def extract_features(mesh):
    if mesh.img_data is None:
        return extract_geometric_features(mesh)
    else:
        return extract_rgb_features(mesh)


def extract_rgb_features(mesh):
    features = []
    n_grid_y, n_grid_x, n_grid_dim = mesh.img_data.shape
    x = np.linspace(0, n_grid_x-1, n_grid_x)
    y = np.linspace(0, n_grid_y-1, n_grid_y)
    f_img_0 = interp2d(x, y, mesh.img_data[:, :, 0])
    f_img_1 = interp2d(x, y, mesh.img_data[:, :, 1])
    f_img_2 = interp2d(x, y, mesh.img_data[:, :, 2])

    for edge in mesh.edges:
        v0 = mesh.vs[edge[0]].astype(float)
        f0 = np.concatenate((f_img_0(v0[0], v0[1]), f_img_1(v0[0], v0[1]),
                             f_img_2(v0[0], v0[1])))
        v1 = mesh.vs[edge[1]].astype(float)
        f1 = np.concatenate((f_img_0(v1[0], v1[1]), f_img_1(v1[0], v1[1]),
                             f_img_2(v1[0], v1[1])))
        feature = np.concatenate((f0, f1))
        features.append(feature)
    return np.array(features).T


def extract_geometric_features(mesh):
    features = []
    edge_points = get_edge_points(mesh)
    set_edge_lengths(mesh, edge_points)
    with np.errstate(divide='raise'):
        try:
            for extractor in [dihedral_angle, symmetric_opposite_angles, diagonal_ratios]:
                feature = extractor(mesh, edge_points)
                features.append(feature)
            return np.concatenate(features, axis=0)
        except Exception as e:
            print(e)
            raise ValueError(mesh.filename, 'bad features')


def dihedral_angle(mesh, edge_points):
    normals_a = get_normals(mesh, edge_points, 0)
    normals_b = get_normals(mesh, edge_points, 5)
    dot = np.sum(normals_a * normals_b, axis=1).clip(-1, 1)
    angles = np.expand_dims(np.pi - np.arccos(dot), axis=0)
    return angles


def symmetric_opposite_angles(mesh, edge_points):
    """ computes two angles: one for each face shared between the edge
        the angle is in each face opposite the edge
        sort handles order ambiguity
    """
    angles_a, angles_b = get_opposite_angles(mesh, edge_points, 0)
    angles_c, angles_d = get_opposite_angles(mesh, edge_points, 5)
    angles = np.concatenate((np.expand_dims(angles_a, 0), np.expand_dims(angles_b, 0),
                            np.expand_dims(angles_c, 0), np.expand_dims(angles_d, 0)), axis=0)
    angles = np.sort(angles, axis=0)
    return angles


def get_diag_ratios(mesh, edge_points, side):

    a_end = edge_points[:, side // 3]
    a_start = edge_points[:, side // 2 + 3]
    b_end = edge_points[:, 1 - side // 3]
    b_start = edge_points[:, side // 2 + 2]

    length_a = np.linalg.norm(mesh.vs[a_end] - mesh.vs[a_start], ord=2, axis=1)
    length_b = np.linalg.norm(mesh.vs[b_end] - mesh.vs[b_start], ord=2, axis=1)
    ratios = length_a / length_b
    return ratios


def diagonal_ratios(mesh, edge_points):
    ratios_a = get_diag_ratios(mesh, edge_points, 0)
    ratios_b = get_diag_ratios(mesh, edge_points, 5)
    ratios = np.concatenate((np.expand_dims(ratios_a, 0), np.expand_dims(ratios_b, 0)), axis=0)
    return np.sort(ratios, axis=0)


def symmetric_ratios(mesh, edge_points):
    """ computes two ratios: one for each face shared between the edge
        the ratio is between the height / base (edge) of each triangle
        sort handles order ambiguity
    """
    ratios_a = get_ratios(mesh, edge_points, 0)
    ratios_b = get_ratios(mesh, edge_points, 3)
    ratios = np.concatenate((np.expand_dims(ratios_a, 0), np.expand_dims(ratios_b, 0)), axis=0)
    return np.sort(ratios, axis=0)


def get_edge_points(mesh):
    """ returns: edge_points (#E x 4) tensor, with four vertex ids per edge
        for example: edge_points[edge_id, 0] and edge_points[edge_id, 1] are the two vertices which define edge_id
        each adjacent face to edge_id has another vertex, which is edge_points[edge_id, 2] or edge_points[edge_id, 3]
    """
    edge_points = np.zeros([mesh.edges_count, 6], dtype=np.int32)
    for edge_id, edge in enumerate(mesh.edges):
        edge_points[edge_id] = get_side_points(mesh, edge_id)
        # edge_points[edge_id, 3:] = mesh.get_side_points(edge_id, 2)
    return edge_points


def get_side_points(mesh, edge_id):
    # if mesh.gemm_edges[edge_id, side] == -1:
    #     return mesh.get_side_points(edge_id, ((side + 2) % 4))
    # else:
    edge_a = mesh.edges[edge_id]

    if mesh.gemm_edges[edge_id, 0] == -1:  # TODO check
        edge_b = mesh.edges[mesh.gemm_edges[edge_id, 3]]
        edge_c = mesh.edges[mesh.gemm_edges[edge_id, 4]]
        edge_d = mesh.edges[mesh.gemm_edges[edge_id, 5]]
    else:
        edge_b = mesh.edges[mesh.gemm_edges[edge_id, 0]]
        edge_c = mesh.edges[mesh.gemm_edges[edge_id, 1]]
        edge_d = mesh.edges[mesh.gemm_edges[edge_id, 2]]
    if mesh.gemm_edges[edge_id, 3] == -1:  # TODO check
        edge_e = mesh.edges[mesh.gemm_edges[edge_id, 0]]
        edge_f = mesh.edges[mesh.gemm_edges[edge_id, 1]]
        edge_g = mesh.edges[mesh.gemm_edges[edge_id, 2]]
    else:
        edge_e = mesh.edges[mesh.gemm_edges[edge_id, 3]]
        edge_f = mesh.edges[mesh.gemm_edges[edge_id, 4]]
        edge_g = mesh.edges[mesh.gemm_edges[edge_id, 5]]
    first_vertex = 0
    second_vertex = 0
    third_vertex = 0
    forth_vertex = 0
    fifth_vertex = 0
    if edge_a[1] in edge_b:
        first_vertex = 1
    if edge_b[1] in edge_c:
        second_vertex = 1
    if edge_d[1] in edge_c:
        third_vertex = 1
    if edge_e[1] in edge_f:
        forth_vertex = 1
    if edge_g[1] in edge_f:
        fifth_vertex = 1

    return [edge_a[first_vertex], edge_a[1 - first_vertex], edge_b[second_vertex],
            edge_d[third_vertex], edge_e[forth_vertex], edge_g[fifth_vertex]]


def get_normals(mesh, edge_points, side):

    a_end = edge_points[:, side // 2 + 2]
    a_start = edge_points[:, side // 3]
    b_end = edge_points[:, 1 - side // 3]
    b_start = edge_points[:, side // 3]

    edge_a = mesh.vs[a_end] - mesh.vs[a_start]
    edge_b = mesh.vs[b_end] - mesh.vs[b_start]
    normals = np.cross(edge_a, edge_b)
    div = fixed_division(np.linalg.norm(normals, ord=2, axis=1), epsilon=0.1)
    normals /= div[:, np.newaxis]
    return normals


def get_opposite_angles(mesh, edge_points, side):

    a_end = edge_points[:, side // 3]
    a_start = edge_points[:, side // 2 + 2]
    b_end = edge_points[:, side // 2 + 3]
    b_start = edge_points[:, side // 2 + 2]
    c_end = edge_points[:, side // 2 + 2]
    c_start = edge_points[:, side // 2 + 3]
    d_end = edge_points[:, 1 - side // 5]
    d_start = edge_points[:, side // 2 + 3]

    edges_a = mesh.vs[a_end] - mesh.vs[a_start]
    edges_b = mesh.vs[b_end] - mesh.vs[b_start]
    edges_c = mesh.vs[c_end] - mesh.vs[c_start]
    edges_d = mesh.vs[d_end] - mesh.vs[d_start]

    edges_a /= fixed_division(np.linalg.norm(edges_a, ord=2, axis=1), epsilon=0.1)[:, np.newaxis]
    edges_b /= fixed_division(np.linalg.norm(edges_b, ord=2, axis=1), epsilon=0.1)[:, np.newaxis]
    edges_c /= fixed_division(np.linalg.norm(edges_c, ord=2, axis=1), epsilon=0.1)[:, np.newaxis]
    edges_d /= fixed_division(np.linalg.norm(edges_d, ord=2, axis=1), epsilon=0.1)[:, np.newaxis]
    dot = np.sum(edges_a * edges_b, axis=1).clip(-1, 1)
    dot2 = np.sum(edges_c * edges_d, axis=1).clip(-1, 1)
    return np.arccos(dot), np.arccos(dot2)
    # return np.arccos(dot)


def get_ratios(mesh, edge_points, side):
    edges_lengths = np.linalg.norm(mesh.vs[edge_points[:, side // 2]] - mesh.vs[edge_points[:, 1 - side // 2]],
                                   ord=2, axis=1)
    point_o = mesh.vs[edge_points[:, side // 2 + 2]]
    point_a = mesh.vs[edge_points[:, side // 2]]
    point_b = mesh.vs[edge_points[:, 1 - side // 2]]
    line_ab = point_b - point_a
    projection_length = np.sum(line_ab * (point_o - point_a), axis=1) / fixed_division(
        np.linalg.norm(line_ab, ord=2, axis=1), epsilon=0.1)
    closest_point = point_a + (projection_length / edges_lengths)[:, np.newaxis] * line_ab
    d = np.linalg.norm(point_o - closest_point, ord=2, axis=1)
    return d / edges_lengths


def fixed_division(to_div, epsilon):
    if epsilon == 0:
        to_div[to_div == 0] = 0.1
    else:
        to_div += epsilon
    return to_div
