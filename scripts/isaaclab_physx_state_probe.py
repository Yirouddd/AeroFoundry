"""Compare USD transforms with Isaac Lab's PhysX-backed rigid-object state."""

import argparse
import json
from pathlib import Path

from isaaclab.app import AppLauncher

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("scene", type=Path)
parser.add_argument("--evidence", type=Path, required=True)
AppLauncher.add_app_launcher_args(parser)
args = parser.parse_args()
launcher = AppLauncher(args)
app = launcher.app

import omni.usd
from isaaclab.assets import RigidObject, RigidObjectCfg
from isaaclab.sim import SimulationCfg, SimulationContext
from pxr import Gf, UsdGeom, UsdPhysics


def main():
    opened = omni.usd.get_context().open_stage(str(args.scene.resolve()))
    app.update()
    stage = omni.usd.get_context().get_stage()
    if not opened or stage is None:
        raise RuntimeError(f"Unable to open scene: {args.scene}")

    probe = UsdGeom.Sphere.Define(stage, "/World/TransientStateProbe")
    probe.CreateRadiusAttr(0.05)
    UsdGeom.Xformable(probe.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(3.0, 0.0, 1.5))
    UsdPhysics.CollisionAPI.Apply(probe.GetPrim())
    UsdPhysics.RigidBodyAPI.Apply(probe.GetPrim())
    UsdPhysics.MassAPI.Apply(probe.GetPrim()).CreateMassAttr(0.2)

    sim = SimulationContext(SimulationCfg(
        physics_prim_path="/World/PhysicsScene",
        dt=1.0 / 240.0,
        render_interval=1,
        device=args.device,
        gravity=(0.0, 0.0, -9.81),
    ))
    rigid = RigidObject(RigidObjectCfg(prim_path="/World/TransientStateProbe", spawn=None))
    sim.reset()
    rigid.update(0.0)
    start_physx = rigid.data.root_pos_w[0].detach().cpu().tolist()
    start_usd = omni.usd.get_world_transform_matrix(probe.GetPrim()).ExtractTranslation()
    for _ in range(120):
        sim.step(render=True)
        rigid.update(sim.get_physics_dt())
    end_physx = rigid.data.root_pos_w[0].detach().cpu().tolist()
    end_usd = omni.usd.get_world_transform_matrix(probe.GetPrim()).ExtractTranslation()

    report = {
        "status": "PASSED" if start_physx != end_physx else "FAILED",
        "scene": str(args.scene.resolve()),
        "steps": 120,
        "sim_time_s": sim.current_time,
        "physx_start_pos_w_m": start_physx,
        "physx_end_pos_w_m": end_physx,
        "usd_start_pos_w_m": [start_usd[0], start_usd[1], start_usd[2]],
        "usd_end_pos_w_m": [end_usd[0], end_usd[1], end_usd[2]],
        "scope": "free-fall rigid-body state-view probe only; not press mechanism or robot contact",
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    app.close()


if __name__ == "__main__":
    main()
