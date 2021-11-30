# Python 2 / 3 support
from __future__ import print_function

import argparse

import ToolRepairSTL
# Local imports
from common import *
from movementPreProcessing import get_point_map
from simulate import run_simulation
from visualize import visualize


def run_pre_processing(filename_model, verbose_print, smoothing_method, smoothing_factor, meshing_method,
                       refine_region, atrium_present, create_flow_extensions, viz, config_path, coarsening_factor,
                       flow_extension_length, edge_length, region_points, dynamic_mesh, clamp_boundaries,
                       compress_mesh):
    """
    Automatically generate mesh of surface model in .vtu and .xml format, including prescribed
    flow rates at inlet and outlet based on flow network model.

    Runs simulation of meshed case on a remote ssh server if server configuration is provided.

    Args:
        filename_model (str): Name of case
        verbose_print (bool): Toggles verbose mode
        smoothing_method (str): Method for surface smoothing
        smoothing_factor (float): Smoothing parameter
        meshing_method (str): Method for meshing
        refine_region (bool): Refines selected region of input if True
        atrium_present (bool): Determines whether this is an atrium case
        create_flow_extensions (bool): Adds flow extensions to mesh if True
        viz (bool): Visualize resulting surface model with flow rates
        config_path (str): Path to configuration file for remote simulation
        coarsening_factor (float): Refine or coarsen the standard mesh size with given factor
        region_points (list): User defined points to define which region to refine
        edge_length (float): Edge length used for meshing with constant element size
        flow_extension_length (float): Factor defining length of flow extensions
        compress_mesh (bool): Compresses finalized mesh if True
        dynamic_mesh (bool): Computes projected movement for displaced surfaces located in [filename_model]_moved folder
        clamp_boundaries (bool): Clamps inlet(s) and outlet if true
    """
    # Get paths
    abs_path = path.abspath(path.dirname(__file__))
    case_name = filename_model.rsplit(path.sep, 1)[-1].rsplit('.')[0]
    dir_path = filename_model.rsplit(path.sep, 1)[0]

    # Naming conventions
    file_name_centerlines = path.join(dir_path, case_name + "_centerlines.vtp")
    file_name_refine_region_centerlines = path.join(dir_path, case_name + "_refine_region_centerline.vtp")
    file_name_region_centerlines = path.join(dir_path, case_name + "_sac_centerline_{}.vtp")
    file_name_distance_to_sphere_diam = path.join(dir_path, case_name + "_distance_to_sphere_diam.vtp")
    file_name_distance_to_sphere_const = path.join(dir_path, case_name + "_distance_to_sphere_const.vtp")
    file_name_distance_to_sphere_curv = path.join(dir_path, case_name + "_distance_to_sphere_curv.vtp")
    file_name_probe_points = path.join(dir_path, case_name + "_probe_point")
    file_name_voronoi = path.join(dir_path, case_name + "_voronoi.vtp")
    file_name_voronoi_smooth = path.join(dir_path, case_name + "_voronoi_smooth.vtp")
    file_name_surface_smooth = path.join(dir_path, case_name + "_smooth.vtp")
    file_name_model_flow_ext = path.join(dir_path, case_name + "_flowext.vtp")
    file_name_clipped_model = path.join(dir_path, case_name + "_clippedmodel.vtp")
    file_name_flow_centerlines = path.join(dir_path, case_name + "_flow_cl.vtp")
    file_name_surface_name = path.join(dir_path, case_name + "_remeshed_surface.vtp")
    file_name_xml_mesh = path.join(dir_path, case_name + ".xml")
    file_name_vtu_mesh = path.join(dir_path, case_name + ".vtu")
    file_name_run_script = path.join(dir_path, case_name + ".sh")
    file_name_displacement_points = path.join(dir_path, case_name + "_points.np")
    folder_moved_surfaces = path.join(dir_path, case_name + "_moved")
    folder_extended_surfaces = path.join(dir_path, case_name + "_extended")

    print("\n--- Working on case:", case_name, "\n")

    # Open the surface file.
    print("--- Load model file\n")
    surface = read_polydata(filename_model)

    if not is_surface_capped(surface) and smoothing_method != "voronoi":
        print("--- Clipping the models inlets and outlets.\n")
        if not path.isfile(file_name_clipped_model):
            # TODO: Add input parameters as input to automatedPreProcessing
            surface = get_uncapped_surface(surface, gradients_limit=0.01, area_limit=20, circleness_limit=5)
            write_polydata(surface, file_name_clipped_model)
        else:
            surface = read_polydata(file_name_clipped_model)
    parameters = get_parameters(path.join(dir_path, case_name))

    if "check_surface" not in parameters.keys():
        surface = vtk_clean_polydata(surface)
        surface = vtk_triangulate_surface(surface)

        # Check the mesh if there is redundant nodes or NaN triangles.
        ToolRepairSTL.surfaceOverview(surface)
        ToolRepairSTL.foundAndDeleteNaNTriangles(surface)
        surface = ToolRepairSTL.cleanTheSurface(surface)
        foundNaN = ToolRepairSTL.foundAndDeleteNaNTriangles(surface)
        if foundNaN:
            raise RuntimeError(("There is an issue with the surface. "
                                "Nan coordinates or some other shenanigans."))
        else:
            parameters["check_surface"] = True
            write_parameters(parameters, path.join(dir_path, case_name))

    # Create a capped version of the surface
    capped_surface = vmtk_cap_polydata(surface)

    # Get centerlines
    print("--- Get centerlines\n")
    inlet, outlets = get_centers_for_meshing(surface, atrium_present, path.join(dir_path, case_name))
    if atrium_present:
        source = outlets
        target = inlet
    else:
        source = inlet
        target = outlets
    centerlines, _, _ = compute_centerlines(source, target, file_name_centerlines, capped_surface, resampling=0.1)
    tol = get_centerline_tolerance(centerlines)

    # Get 'center' and 'radius' of the regions(s)
    region_center = []
    misr_max = []

    if refine_region:
        regions = get_regions_to_refine(capped_surface, region_points, path.join(dir_path, case_name))

        centerlineAnu, _, _ = compute_centerlines(source, regions, file_name_refine_region_centerlines, capped_surface,
                                                  resampling=0.1)

        # Extract the region centerline
        refine_region_centerline = []
        info = get_parameters(path.join(dir_path, case_name))
        num_anu = info["number_of_regions"]

        # Compute mean distance between points
        for i in range(num_anu):
            if not path.isfile(file_name_region_centerlines.format(i)):
                line = extract_single_line(centerlineAnu, i)
                locator = get_vtk_point_locator(centerlines)
                for j in range(line.GetNumberOfPoints() - 1, 0, -1):
                    point = line.GetPoints().GetPoint(j)
                    ID = locator.FindClosestPoint(point)
                    tmp_point = centerlines.GetPoints().GetPoint(ID)
                    dist = np.sqrt(np.sum((np.asarray(point) - np.asarray(tmp_point)) ** 2))
                    if dist <= tol:
                        break

                tmp = extract_single_line(line, 0, start_id=j)
                write_polydata(tmp, file_name_region_centerlines.format(i))

                # List of VtkPolyData sac(s) centerline
                refine_region_centerline.append(tmp)

            else:
                refine_region_centerline.append(read_polydata(file_name_region_centerlines.format(i)))

        # Merge the sac centerline
        region_centerlines = vtk_merge_polydata(refine_region_centerline)

        for region in refine_region_centerline:
            region_factor = 0.9 if atrium_present else 0.5
            region_center.append(region.GetPoints().GetPoint(int(region.GetNumberOfPoints() * region_factor)))
            tmp_misr = get_point_data_array(radiusArrayName, region)
            misr_max.append(tmp_misr.max())

    # Smooth surface
    if smoothing_method == "voronoi":
        print("--- Smooth surface: Voronoi smoothing\n")
        if not path.isfile(file_name_surface_smooth):
            # Get Voronoi diagram
            if not path.isfile(file_name_voronoi):
                voronoi = make_voronoi_diagram(surface, file_name_voronoi)
                write_polydata(voronoi, file_name_voronoi)
            else:
                voronoi = read_polydata(file_name_voronoi)

            # Get smooth Voronoi diagram
            if not path.isfile(file_name_voronoi_smooth):
                if refine_region:
                    smooth_voronoi = smooth_voronoi_diagram(voronoi, centerlines, smoothing_factor, region_centerlines)
                else:
                    smooth_voronoi = smooth_voronoi_diagram(voronoi, centerlines, smoothing_factor)

                write_polydata(smooth_voronoi, file_name_voronoi_smooth)
            else:
                smooth_voronoi = read_polydata(file_name_voronoi_smooth)

            # Envelope the smooth surface
            surface = create_new_surface(smooth_voronoi)

            # Uncapp the surface
            surface_uncapped = get_uncapped_surface(surface)

            # Check if there has been added new outlets
            num_outlets = centerlines.GetNumberOfLines()
            num_outlets_after = compute_centers(surface_uncapped, atrium_present, test_capped=True)[1]

            if num_outlets != num_outlets_after:
                surface = vmtk_smooth_surface(surface, "laplace", iterations=200)
                write_polydata(surface, file_name_surface_smooth)
                print(("ERROR: Automatic clipping failed. You have to open {} and " +
                       "manually clipp the branch which still is capped. " +
                       "Overwrite the current {} and restart the script.").format(
                    file_name_surface_smooth, file_name_surface_smooth))
                sys.exit(0)

            surface = surface_uncapped

            # Smoothing to improve the quality of the elements
            # Consider to add a subdivision here as well.
            surface = vmtk_smooth_surface(surface, "laplace", iterations=200)

            # Write surface
            write_polydata(surface, file_name_surface_smooth)

        else:
            surface = read_polydata(file_name_surface_smooth)

    elif smoothing_method in ["laplace", "taubin"]:
        print("--- Smooth surface: {} smoothing\n".format(smoothing_method.capitalize()))
        if not path.isfile(file_name_surface_smooth):
            surface = vmtk_smooth_surface(surface, smoothing_method, iterations=400)

            # Save the smoothed surface
            write_polydata(surface, file_name_surface_smooth)

        else:
            surface = read_polydata(file_name_surface_smooth)

    elif smoothing_method == "no_smooth" or None:
        print("--- No smoothing of surface\n")

    # Add flow extensions
    if create_flow_extensions:
        if not path.isfile(file_name_model_flow_ext):
            print("--- Adding flow extensions\n")
            # Add extension normal on boundary for atrium models
            extension = "boundarynormal" if atrium_present else "centerlinedirection"
            surface_extended = add_flow_extension(surface, centerlines, include_outlet=False,
                                                  extension_length=flow_extension_length)
            surface_extended = add_flow_extension(surface_extended, centerlines, include_outlet=True,
                                                  extension_length=flow_extension_length, extension_mode=extension)

            write_polydata(surface_extended, file_name_model_flow_ext)

        else:
            surface_extended = read_polydata(file_name_model_flow_ext)
    else:
        surface_extended = surface

    # Smooth and capp surface with flow extensions
    surface_extended = vmtk_smooth_surface(surface_extended, "laplace", iterations=200)
    capped_surface = vmtk_cap_polydata(surface_extended)

    if dynamic_mesh:
        # Get a point mapper
        distance, point_map = get_point_map(surface, surface_extended)

        # Project displacement between surfaces
        points = project_displacement(clamp_boundaries, distance, folder_extended_surfaces, folder_moved_surfaces,
                                      point_map, surface, surface_extended)

        # Save displacement to numpy array
        save_displacement(file_name_displacement_points, points)

    # Get new centerlines with the flow extensions
    if create_flow_extensions:
        if not path.isfile(file_name_flow_centerlines):
            print("--- Compute the model centerlines with flow extension.\n")
            # Compute the centerlines. FIXIT: There are several inlets and one outet for atrium case
            inlet, outlets = get_centers_for_meshing(surface_extended, atrium_present, path.join(dir_path, case_name),
                                                     flowext=True)
            if atrium_present:
                source = outlets
                target = inlet
            else:
                source = inlet
                target = outlets
            centerlines, _, _ = compute_centerlines(source, target, file_name_flow_centerlines, capped_surface,
                                                    resampling=0.1)

        else:
            centerlines = read_polydata(file_name_flow_centerlines)

    # Choose input for the mesh
    print("--- Computing distance to sphere\n")
    if meshing_method == "constant":
        if not path.isfile(file_name_distance_to_sphere_const):
            distance_to_sphere = dist_sphere_constant(surface_extended, centerlines, region_center, misr_max,
                                                      file_name_distance_to_sphere_const, edge_length)
        else:
            distance_to_sphere = read_polydata(file_name_distance_to_sphere_const)

    elif meshing_method == "curvature":
        if not path.isfile(file_name_distance_to_sphere_curv):
            distance_to_sphere = dist_sphere_curv(surface_extended, centerlines, region_center, misr_max,
                                                  file_name_distance_to_sphere_curv, coarsening_factor)
        else:
            distance_to_sphere = read_polydata(file_name_distance_to_sphere_curv)
    elif meshing_method == "diameter":
        if not path.isfile(file_name_distance_to_sphere_diam):
            distance_to_sphere = dist_sphere_diam(surface_extended, centerlines, region_center, misr_max,
                                                  file_name_distance_to_sphere_diam, coarsening_factor)
        else:
            distance_to_sphere = read_polydata(file_name_distance_to_sphere_diam)

    # Compute mesh
    if not path.isfile(file_name_vtu_mesh):
        try:
            print("--- Computing mesh\n")
            mesh, remeshed_surface = generate_mesh(distance_to_sphere)
            assert remeshed_surface.GetNumberOfPoints() > 0, \
                "No points in surface mesh, try to remesh"
            assert mesh.GetNumberOfPoints() > 0, "No points in mesh, try to remesh"

        except:
            distance_to_sphere = mesh_alternative(distance_to_sphere)
            mesh, remeshed_surface = generate_mesh(distance_to_sphere)
            assert mesh.GetNumberOfPoints() > 0, "No points in mesh, after remeshing"
            assert remeshed_surface.GetNumberOfPoints() > 0, \
                "No points in surface mesh, try to remesh"

        polyDataVolMesh = write_mesh(compress_mesh, file_name_surface_name, file_name_vtu_mesh, file_name_xml_mesh,
                                     mesh, remeshed_surface)

    else:
        polyDataVolMesh = read_polydata(file_name_vtu_mesh)

    network, probe_points = setup_model_network(centerlines, file_name_probe_points, region_center, verbose_print)

    # BSL method for mean inlet flow rate.
    parameters = get_parameters(path.join(dir_path, case_name))

    print("--- Computing flow rates and flow split, and setting boundary IDs\n")
    mean_inflow_rate = compute_flow_rate(atrium_present, inlet, parameters)

    find_boundaries(path.join(dir_path, case_name), mean_inflow_rate, network, polyDataVolMesh, verbose_print)

    # Display the flow split at the outlets, inlet flow rate, and probes.
    if viz:
        visualize(network.elements, probe_points, surface_extended, mean_inflow_rate)

    # Start simulation though ssh, without password
    if config_path is not None:

        # Set up simulation script
        if not path.exists(file_name_run_script):
            run_script_sample = open(path.join(abs_path, "run_script.sh"), "r").read()
            config = json.load(open(config_path))
            run_dict = dict(mesh_name=case_name,
                            num_nodes=1,
                            hours=120,
                            account="nn9249k",
                            remoteFolder=config["remoteFolder"],
                            results_folder="results")
            run_script = run_script_sample.format(**run_dict)

            # Write script
            script_file = open(file_name_run_script, "w")
            script_file.write(run_script)
            script_file.close()

        run_simulation(config_path, dir_path, case_name)


def read_command_line():
    """
    Read arguments from commandline and return all values in a dictionary.
    """
    '''Command-line arguments.'''
    parser = argparse.ArgumentParser(
        description="Automatic pre-processing for FEniCS.")

    parser.add_argument('-v', '--verbosity',
                        dest='verbosity',
                        type=str2bool,
                        default=False,
                        help="Activates the verbose mode.")

    parser.add_argument('-i', '--inputModel',
                        type=str,
                        required=False,
                        dest='fileNameModel',
                        default='example/surface.vtp',
                        help="Input file containing the 3D model.")

    parser.add_argument('-cM', '--compress-mesh',
                        type=str2bool,
                        required=False,
                        dest='compressMesh',
                        default=True,
                        help="Compress output mesh after generation.")

    parser.add_argument('-sM', '--smoothingMethod',
                        type=str,
                        required=False,
                        dest='smoothingMethod',
                        default="no_smooth",
                        choices=["voronoi", "no_smooth", "laplace", "taubin"],
                        help="Smoothing method, for now only Voronoi smoothing is available." +
                             " For Voronoi smoothing you can also control smoothingFactor" +
                             " (default = 0.25).")

    parser.add_argument('-c', '--coarseningFactor',
                        type=float,
                        required=False,
                        dest='coarseningFactor',
                        default=1.0,
                        help="Refine or coarsen the standard mesh size. The higher the value the coarser the mesh.")

    parser.add_argument('-sF', '--smoothingFactor',
                        type=float,
                        required=False,
                        dest='smoothingFactor',
                        default=0.25,
                        help="smoothingFactor for VoronoiSmoothing, removes all spheres which" +
                             " has a radius < MISR*(1-0.25), where MISR varying along the centerline.")

    parser.add_argument('-m', '--meshingMethod',
                        dest="meshingMethod",
                        type=str,
                        choices=["diameter", "curvature", "constant"],
                        default="diameter")

    parser.add_argument('-el', '--edge-length',
                        dest="edgeLength",
                        default=None,
                        type=float,
                        help="Characteristic edge length used for meshing.")

    parser.add_argument('-r', '--refine-region',
                        dest="refineRegion",
                        type=str2bool,
                        default=False,
                        help="Determine weather or not to refine a specific region of " +
                             "the input model. Default is False.")

    parser.add_argument('-rp', '--region-points',
                        dest="regionPoints",
                        type=float,
                        nargs="+",
                        default=None,
                        help="If -r or --refine-region is True, the user can provide the point(s)"
                             " which defines the regions to refine. " +
                             "Example providing the points (0.1, 5.0, -1) and (1, -5.2, 3.21):" +
                             " --region-points 0.1 5 -1 1 5.24 3.21")

    parser.add_argument('-at', '--atrium',
                        dest="atriumPresent",
                        type=str2bool,
                        default=False,
                        help="Determine weather or not the model is an Atrium model. Default is False.")

    parser.add_argument('-f', '--flowext',
                        dest="flowExtension",
                        default=True,
                        type=str2bool,
                        help="Add flow extensions to to the model.")

    parser.add_argument('-fl', '--flowextlen',
                        dest="flowExtLen",
                        default=5,
                        type=float,
                        help="Length of flow extensions.")

    parser.add_argument('-dm', '--dynamic-mesh',
                        dest="dynamicMesh",
                        default=False,
                        type=str2bool,
                        help="If true, assumes a dynamic mesh and will perform computation of projection " +
                             "between moved surfaces located in the '[filename_model]_moved' folder.")

    parser.add_argument('-cl', '--clamp-boundaries',
                        dest="clampBoundaries",
                        default=False,
                        type=str2bool,
                        help="Clamps boundaries at inlet(s) and outlet if true.")

    parser.add_argument('-vz', '--visualize',
                        dest="viz",
                        default=True,
                        type=str2bool,
                        help="Visualize surface, inlet, outlet and probes after meshing.")

    parser.add_argument('--simulationConfig',
                        type=str,
                        dest="config",
                        default=None,
                        help='Path to configuration file for remote simulation. ' +
                             'See example/ssh_config.json for details')

    args, _ = parser.parse_known_args()

    if args.verbosity:
        print()
        print("--- VERBOSE MODE ACTIVATED ---")

        def verbose_print(*args):
            for arg in args:
                print(arg, end=' ')
                print()
    else:
        verbose_print = lambda *a: None

    verbose_print(args)

    return dict(filename_model=args.fileNameModel, verbose_print=verbose_print, smoothing_method=args.smoothingMethod,
                smoothing_factor=args.smoothingFactor, meshing_method=args.meshingMethod,
                refine_region=args.refineRegion, atrium_present=args.atriumPresent,
                create_flow_extensions=args.flowExtension, viz=args.viz, config_path=args.config,
                coarsening_factor=args.coarseningFactor, flow_extension_length=args.flowExtLen,
                edge_length=args.edgeLength, region_points=args.regionPoints, dynamic_mesh=args.dynamicMesh,
                clamp_boundaries=args.clampBoundaries, compress_mesh=args.compressMesh)


if __name__ == "__main__":
    run_pre_processing(**read_command_line())
