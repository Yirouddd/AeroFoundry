"""Build and validate a scene under the Isaac Lab launcher."""

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

from isaaclab.app import AppLauncher

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("job", type=Path)
AppLauncher.add_app_launcher_args(parser)
args = parser.parse_args()
print("[asset-scene] launching Isaac Lab", flush=True)
app_launcher = AppLauncher(args)
app = app_launcher.app
print("[asset-scene] Isaac Lab app ready", flush=True)

import omni.usd
from isaaclab.assets import RigidObject, RigidObjectCfg
from isaaclab.sim import SimulationCfg, SimulationContext
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade


def attr(prim, name, typ, value):
    prim.CreateAttribute(name, typ).Set(value)


def translate(prim, xyz):
    UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(*xyz))


def color(geom, rgb):
    geom.CreateDisplayColorAttr([Gf.Vec3f(*rgb)])


def cube(stage, path, pos, size, rgb, collision=True, rigid=False):
    geom = UsdGeom.Cube.Define(stage, path)
    geom.CreateSizeAttr(1.0)
    translate(geom.GetPrim(), pos)
    UsdGeom.Xformable(geom.GetPrim()).AddScaleOp().Set(Gf.Vec3f(*size))
    color(geom, rgb)
    if collision:
        UsdPhysics.CollisionAPI.Apply(geom.GetPrim())
    if rigid:
        UsdPhysics.RigidBodyAPI.Apply(geom.GetPrim())
    return geom.GetPrim()


def make_obj_mesh(stage, path, obj_file):
    vertices, faces = [], []
    for line in Path(obj_file).read_text(encoding="utf-8").splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "v":
            x, y, z = map(float, tokens[1:4])
            vertices.append(Gf.Vec3f(x, -z, y))
        elif tokens[0] == "f":
            ix = [int(t.split("/")[0]) - 1 for t in tokens[1:]]
            for i in range(1, len(ix) - 1):
                faces.append((ix[0], ix[i], ix[i + 1]))
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(vertices)
    mesh.CreateFaceVertexCountsAttr([3] * len(faces))
    mesh.CreateFaceVertexIndicesAttr([i for f in faces for i in f])
    mesh.CreateSubdivisionSchemeAttr("none")
    color(mesh, (0.37, 0.35, 0.72))
    return {"vertices": len(vertices), "triangles": len(faces),
            "local_bounds_m": [[min(v[i] for v in vertices), max(v[i] for v in vertices)] for i in range(3)]}


def build(job):
    scene_path = Path(job["scene_path"])
    print("[asset-scene] creating scene stage", flush=True)
    stage = Usd.Stage.CreateNew(str(scene_path))
    print("[asset-scene] scene stage created", flush=True)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    scene.CreateGravityMagnitudeAttr(9.81)
    friction_material = UsdShade.Material.Define(stage, "/World/PhysicsMaterial")
    physics_material = UsdPhysics.MaterialAPI.Apply(friction_material.GetPrim())
    physics_material.CreateStaticFrictionAttr(0.5)
    physics_material.CreateDynamicFrictionAttr(0.4)
    physics_material.CreateRestitutionAttr(0.0)

    robot_placement = UsdGeom.Xform.Define(stage, "/World/Robot")
    translate(robot_placement.GetPrim(), (-1.5, 0, 1.0))
    robot = UsdGeom.Xform.Define(stage, "/World/Robot/Asset")
    print("[asset-scene] adding robot reference", flush=True)
    robot.GetPrim().GetReferences().AddReference(str(Path(job["robot_asset"]).resolve()))
    print("[asset-scene] robot reference authored", flush=True)

    print("[asset-scene] adding ground and support", flush=True)
    cube(stage, "/World/Ground", (0, 0, -0.05), (8, 8, 0.1), (0.38, 0.38, 0.38))
    support = cube(stage, "/World/FixedSupport", (1.5, 0, 0.25), (0.8, 0.8, 0.5), (0.30, 0.31, 0.33))
    UsdShade.MaterialBindingAPI.Apply(support).Bind(friction_material, materialPurpose="physics")
    base = UsdGeom.Xform.Define(stage, "/World/Target")
    translate(base.GetPrim(), (1.5, 0, 0.5))
    UsdPhysics.RigidBodyAPI.Apply(base.GetPrim()).CreateKinematicEnabledAttr(True)
    print("[asset-scene] importing source mesh", flush=True)
    visual = make_obj_mesh(stage, "/World/Target/SourceVisual", job["source_obj"])
    print("[asset-scene] source mesh imported", flush=True)
    base_collider = cube(stage, "/World/Target/BaseCollider", (0, 0, 0.05), (0.5, 0.5, 0.1), (0.31, 0.28, 0.6))
    UsdShade.MaterialBindingAPI.Apply(base_collider).Bind(friction_material, materialPurpose="physics")

    cap = UsdGeom.Cylinder.Define(stage, "/World/Target/PressCap")
    cap.CreateRadiusAttr(0.12)
    cap.CreateHeightAttr(0.035)
    cap.CreateAxisAttr(UsdGeom.Tokens.z)
    cap_xform = UsdGeom.Xformable(cap.GetPrim())
    cap_xform.SetResetXformStack(True)
    cap_xform.AddTranslateOp().Set(Gf.Vec3d(1.5, 0, 0.6675))
    color(cap, (1.0, 0.75, 0.30))
    UsdPhysics.CollisionAPI.Apply(cap.GetPrim())
    UsdShade.MaterialBindingAPI.Apply(cap.GetPrim()).Bind(friction_material, materialPurpose="physics")
    UsdPhysics.RigidBodyAPI.Apply(cap.GetPrim())
    mass = 0.05  # engineering assumption, not source metadata
    inertia_xy = mass * (3 * 0.12**2 + 0.035**2) / 12
    inertia_z = mass * 0.12**2 / 2
    m = UsdPhysics.MassAPI.Apply(cap.GetPrim())
    m.CreateMassAttr(mass)
    m.CreateDiagonalInertiaAttr(Gf.Vec3f(inertia_xy, inertia_xy, inertia_z))
    m.CreateCenterOfMassAttr(Gf.Vec3f(0, 0, 0))

    joint = UsdPhysics.PrismaticJoint.Define(stage, "/World/Target/PressJoint")
    joint.CreateAxisAttr(UsdPhysics.Tokens.z)
    joint.CreateLowerLimitAttr(-0.02)
    joint.CreateUpperLimitAttr(0.0)
    joint.CreateBody0Rel().SetTargets([Sdf.Path("/World/Target")])
    joint.CreateBody1Rel().SetTargets([Sdf.Path("/World/Target/PressCap")])
    joint.CreateLocalPos1Attr(Gf.Vec3f(0, 0, 0))
    joint.CreateLocalPos0Attr(Gf.Vec3f(0, 0, 0.1675))
    drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), "linear")
    drive.CreateTypeAttr("force")
    drive.CreateStiffnessAttr(200.0)
    drive.CreateDampingAttr(12.0)
    drive.CreateTargetPositionAttr(0.0)
    drive.CreateMaxForceAttr(30.0)
    joint.GetPrim().CreateAttribute("user:triggerThresholdM", Sdf.ValueTypeNames.Double).Set(0.005)
    joint.GetPrim().CreateAttribute("user:triggerDefinition", Sdf.ValueTypeNames.String).Set("joint displacement from released state >= 0.005 m")
    print("[asset-scene] saving scene stage", flush=True)
    stage.GetRootLayer().Save()
    print("[asset-scene] scene stage saved", flush=True)
    return visual


def verify(job, visual):
    path = job["scene_path"]
    ctx = omni.usd.get_context()
    opened = ctx.open_stage(path)
    app.update()
    stage = ctx.get_stage()
    robot_children = [p.GetPath().pathString for p in stage.Traverse() if p.GetPath().pathString.startswith("/World/Robot")][:20]
    robot_stage = Usd.Stage.Open(job["robot_asset"])
    robot_source = {"default_prim": str(robot_stage.GetDefaultPrim().GetPath()) if robot_stage.GetDefaultPrim() else None,
                    "root_children": [p.GetPath().pathString for p in robot_stage.GetPseudoRoot().GetChildren()],
                    "composed_children": [p.GetPath().pathString for p in robot_stage.Traverse()][:10]}
    checks = {"open_stage": bool(opened) and stage is not None,
              "robot_reference_loaded": len(robot_children) > 1,
              "source_visual_loaded": stage.GetPrimAtPath("/World/Target/SourceVisual").IsValid(),
              "support_collision": stage.GetPrimAtPath("/World/FixedSupport").HasAPI(UsdPhysics.CollisionAPI),
              "cap_collision": stage.GetPrimAtPath("/World/Target/PressCap").HasAPI(UsdPhysics.CollisionAPI),
              "joint_exists": stage.GetPrimAtPath("/World/Target/PressJoint").IsValid(),
              "stage_units_m": abs(UsdGeom.GetStageMetersPerUnit(stage) - 1.0) < 1e-9,
              "source_geometry_reasonable": visual["local_bounds_m"][0][1] <= 1.0}
    probe = UsdGeom.Sphere.Define(stage, "/World/TransientPressProbe")
    probe.CreateRadiusAttr(0.035)
    translate(probe.GetPrim(), (3.0, 0, 2.0))
    UsdPhysics.CollisionAPI.Apply(probe.GetPrim())
    UsdPhysics.RigidBodyAPI.Apply(probe.GetPrim())
    UsdPhysics.MassAPI.Apply(probe.GetPrim()).CreateMassAttr(0.4)
    sim = SimulationContext(SimulationCfg(
        physics_prim_path="/World/PhysicsScene",
        dt=1.0 / 240.0,
        render_interval=1,
        device=args.device,
        gravity=(0.0, 0.0, -9.81),
    ))
    cap_state = RigidObject(RigidObjectCfg(prim_path="/World/Target/PressCap", spawn=None))
    probe_state = RigidObject(RigidObjectCfg(prim_path="/World/TransientPressProbe", spawn=None))
    sim.reset()
    dt = sim.get_physics_dt()

    def step_and_update(count):
        for _ in range(count):
            sim.step(render=True)
            cap_state.update(dt)
            probe_state.update(dt)

    def body_position(body):
        return body.data.root_pos_w[0].detach().cpu().tolist()

    for _ in range(30):
        step_and_update(1)
    cap = stage.GetPrimAtPath("/World/Target/PressCap")
    joint = stage.GetPrimAtPath("/World/Target/PressJoint")
    before_pose = body_position(cap_state)
    before = before_pose[2]
    step_and_update(30)
    quiet_pose = body_position(cap_state)
    quiet_z = quiet_pose[2]
    UsdPhysics.DriveAPI.Get(joint, "linear").GetTargetPositionAttr().Set(-0.012)
    step_and_update(120)
    pressed_pose = body_position(cap_state)
    pressed_z = pressed_pose[2]
    displacement = before - pressed_z
    press_step_index = sim.current_time_step_index
    press_sim_time = sim.current_time
    UsdPhysics.DriveAPI.Get(joint, "linear").GetTargetPositionAttr().Set(0.0)
    step_and_update(120)
    released_pose = body_position(cap_state)
    released_z = released_pose[2]
    return_distance = abs(released_z - before)
    import torch

    probe_pose = torch.tensor([[1.5, 0.0, 0.75, 1.0, 0.0, 0.0, 0.0]], device=sim.device)
    probe_velocity = torch.zeros((1, 6), device=sim.device)
    probe_state.write_root_pose_to_sim(probe_pose)
    probe_state.write_root_link_velocity_to_sim(probe_velocity)
    probe_state.update(0.0)
    probe_start_z = body_position(probe_state)[2]
    contact_baseline_z = body_position(cap_state)[2]
    minimum_cap_z = contact_baseline_z
    for _ in range(120):
        step_and_update(1)
        minimum_cap_z = min(minimum_cap_z, body_position(cap_state)[2])
    contact_displacement = contact_baseline_z - minimum_cap_z
    stage.RemovePrim("/World/TransientPressProbe")
    checks["reset_succeeded"] = True
    checks["physics_steps_advanced"] = press_step_index >= 180 and press_sim_time > 0.0
    checks["target_stable_before_press"] = abs(quiet_z - before) < 0.001
    checks["press_motion_measured"] = displacement >= 0.005
    checks["collision_probe_press_measured"] = contact_displacement >= 0.005
    checks["release_return_measured"] = return_distance < 0.003
    checks["no_initial_target_support_penetration"] = abs((0.5 + visual["local_bounds_m"][2][0]) - (0.25 + 0.25)) < 1e-6
    constraints = job["task"]["scene_constraint_spec"]
    checks["scene_constraints_satisfied"] = all(constraints.get(key) in (None, [], "") for key in
                                                ("raw_text", "target_side", "workspace_box_m", "forbidden_regions",
                                                 "support_relation", "asset_ids"))
    structural_checks = {key: value for key, value in checks.items()
                         if key not in {"physics_steps_advanced", "press_motion_measured",
                                        "collision_probe_press_measured", "release_return_measured"}}
    return {"required_checks_passed": all(structural_checks.values()),
            "structural_checks_passed": all(structural_checks.values()),
            "physics_mechanics_checks_passed": all(checks[key] for key in
                                                    ("physics_steps_advanced", "press_motion_measured",
                                                     "collision_probe_press_measured", "release_return_measured")),
            "checks": checks, "physics_step_index_before_probe_reset": press_step_index,
            "physics_sim_time_before_probe_reset_s": press_sim_time,
            "robot_prim_paths_sample": robot_children,
            "robot_source_debug": robot_source,
            "press": {"method": "Isaac Lab joint-drive excitation; no robot contact applied",
                      "released_cap_world_z_m": before, "quiet_cap_world_z_m": quiet_z,
                      "pressed_cap_world_z_m": pressed_z,
                      "pressed_cap_physx_world_xyz_m": pressed_pose,
                      "displacement_m": displacement, "returned_cap_world_z_m": released_z,
                      "returned_cap_physx_world_xyz_m": released_pose,
                      "return_error_m": return_distance, "task_success_threshold_m": 0.005,
                      "state_source": "Isaac Lab RigidObject PhysX view; USD stage transform is not used for live state",
                      "probe_start_world_z_m": probe_start_z,
                      "collision_probe_mass_kg": 0.4, "collision_probe_displacement_m": contact_displacement,
                      "collision_probe_note": "temporary falling sphere, removed from delivered scene; not robot contact",
                      "authored_joint_limit_m": 0.02, "joint_limit_basis": "engineering assumption; not measured asset travel"},
            "unverified": ["robot contact press", "robot hover/thrust", "robot payload", "robot reachability",
                           "contact force", "full scene penetration scan"]}


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main():
    print("[asset-scene] reading job", flush=True)
    job = json.loads(args.job.read_text(encoding="utf-8"))
    print("[asset-scene] building scene", flush=True)
    visual = build(job)
    print("[asset-scene] verifying scene", flush=True)
    verification = verify(job, visual)
    tid = job["task_id"]
    output = {"isaac_version": package_version("isaacsim"),
              "isaac_lab_version": package_version("isaaclab"),
              "launch_backend": "isaaclab.AppLauncher",
              "runtime": {"python": sys.executable, "device": args.device},
              "verification": verification,
              "target_object_instance": {"task_id": tid, "path": "/World/Target", "role": "target",
                                         "source_visual": "/World/Target/SourceVisual", "pressable_part": "/World/Target/PressCap",
                                         "joint": "/World/Target/PressJoint", "source_mesh": visual},
              "scene_spec": {"task_id": tid, "roles": {"robot": "/World/Robot", "target": "/World/Target",
                                                         "fixed_support": "/World/FixedSupport", "ground": "/World/Ground"},
                             "initial_positions_m": {"robot": [-1.5, 0, 1.0], "target": [1.5, 0, 0.5]},
                             "constraints": job["task"]["scene_constraint_spec"]},
              "realized_scene": {"task_id": tid, "scene_usd": job["scene_path"],
                                 "reopened_in_isaac_sim": verification["checks"]["open_stage"],
                                 "physics_assumptions": {"cap_mass_kg": 0.05, "cap_radius_m": 0.12,
                                                         "cap_height_m": 0.035, "joint_travel_m": 0.02,
                                                         "spring_stiffness_N_per_m": 200.0, "damping_Ns_per_m": 12.0,
                                                         "static_friction": 0.5, "dynamic_friction": 0.4,
                                                         "friction_basis": "engineering configuration; not asset metadata"},
                                 "facts": {"source_obj": job["source_obj"], "source_mesh_bounds_m": visual["local_bounds_m"]}}}
    out = Path(job["scene_dir"]) / "isaac_evidence.json"
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(out), "checks": verification["checks"]}))


try:
    main()
finally:
    SimulationContext.clear_instance()
    app.close(wait_for_replicator=False)
