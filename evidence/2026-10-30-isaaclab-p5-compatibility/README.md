# Isaac Lab Linux Compatibility Trial

Execution date: 2026-10-31, Asia/Singapore. The directory name follows the first launch's UTC-side date; Kit log timestamps are UTC.

## Scope and final results

- Runtime: Isaac Sim `5.1.0.0`, Isaac Lab `0.48.0`, Isaac Lab source tree `2.3.1`, Python 3.11, CUDA `cuda:0`.
- P5 asset smoke: **PASSED**. Converted the repository M500 URDF with a fixed base, preserved all six arm joints `A-F` and `F_Link`, composed the USD, reset, and advanced 122 PhysX steps (`0.5083 s`, `dt=1/240 s`). Source URDF SHA-256: `d0f0ba92085ff2816421432eab6105e9307623b30d43b582088da95b0f7b5b81`; converted USD SHA-256: `35a9a17806622ba513724b2a2992858ad9a078618609f86f7d88aaef2e2dee8c`.
- AeroFoundry Asset & Scene smoke: scene construction, reopening, P5 USD composition, scene units, collision/schema presence, simulation clock, and repaired synthetic mechanism checks **PASSED**. PhysX state-view measurements: press-cap drive `0.0122319 m`, probe-induced cap displacement `0.0177004 m`, and release error `0.0002351 m`. This validates the scene's synthetic press joint/collision propagation only, not robot contact.
- No HQP controller, EE trajectory, PX4 vehicle, TCP/frame transform, flight, task tracking, contact force, or real-time constraint was tested.
- AeroFoundry Python test suite: **27/27 passed** using the repository Python 3.13 editable environment with `usd-core==26.8`.

## Final evidence

- `report-final.json`: P5 URDF-to-USD and PhysX clock smoke.
- `console-instrumented.log`: captured Isaac Lab startup and all P5 smoke checkpoints.
- `asset-scene-report-final.json`: initial structural pass with zero-motion mechanism result; preserved as a failed attempt.
- `asset-scene-report-rigidview-fix.json`: repaired scene smoke; `verification.physics_mechanics_checks_passed=true`. This is synthetic mechanism validation, not P5 contact.
- `physx-state-view-probe.json` and `physx-state-view-probe.log`: confirms USD stage transforms remain stale while the PhysX-backed rigid-object state advances.
- Full generated scene files, Kit logs, and raw subprocess output remain in the ignored `runs/` directory alongside the checkout.

## Attempts and preserved failures

| Artifact | Outcome |
| --- | --- |
| `console.log` | Initial P5 launch hit `CXXABI_1.3.15 not found` because the system `libstdc++` preceded the Isaac conda runtime. A retry redirected over this file before the first log was preserved; the original terminal failure is recorded here but its original console bytes are unavailable. |
| `console-rerun.log` | Runtime library path was corrected; URDF conversion and scene startup reached the simulator but the attempt stalled before checkpoints and was manually stopped. Its generated USD remains under `runs/`. |
| `report-instrumented.json` | P5 URDF conversion, joint/link preservation, reset, and 120 requested steps passed. A later final smoke added actual PhysX clock/index assertions; see `report-final.json`. |
| `asset-scene-report.json`, `asset-scene-report-instrumented.json`, `asset-scene-report-trace.json` | Runner returned exit code 0 without writing its evidence file. Stage logs localized the exit to scene construction after the robot reference was applied. The transform was moved to a dedicated parent placement prim; subsequent attempts reached validation. |
| `asset-scene-report-wrapper.json`, `asset-scene-report-timeline.json`, `asset-scene-report-render-sync.json`, `asset-scene-report-stepdiag.json`, `asset-scene-report-final.json` | Structural validation passed after the wrapper fix. The candidate joint-drive and collision-probe mechanics remained at zero measured displacement. Timeline and render synchronization changes did not fix those mechanism checks. |
| `asset-scene-report-rigid-state.json` | A follow-up PhysX rigid-view readback attempt consumed sustained CPU and produced no progress for over five minutes; the runner was terminated with signal 9. Failure result is preserved. |
| `physx-state-view-probe.json` | Isaac Lab `RigidObject` state measured a free-fall body moving from `z=1.4995 m` to `z=0.2221 m` in 120 steps, while the USD stage transform stayed at `z=1.5 m`. This confirmed that live state must be read from PhysX rather than stage transforms under the active Fabric configuration. |
| `asset-scene-report-rigidview-fix.json` | Added the missing PressCap xform-stack reset and switched verification to `RigidObject` PhysX state. Joint drive, release, and collision-probe checks passed; the probe is synthetic and no robot contact was applied. |
| Direct system-Python P5 launch | Failed before startup with `ModuleNotFoundError: No module named 'isaaclab'`; the simulator must run in the Isaac Lab Python environment. |

The workspace is a Linux checkout on `linux-isaaclab-backend`. Example requests now use environment-expanded Linux paths, the pipeline accepts an explicit Isaac Python executable, and its subprocess environment prepends that environment's `lib` directory to `LD_LIBRARY_PATH`. Final mechanism validation reads PhysX state through Isaac Lab `RigidObject`; it does not infer live motion from USD stage transforms.
