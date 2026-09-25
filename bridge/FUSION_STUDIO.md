# CyberFinger Fusion Studio

A single-window application that fuses three sensor streams into one live picture of the body, the arm and the whole
hand:

| source | hardware | what it contributes |
|---|---|---|
| headset camera | Quest hand tracking through OpenXR | 26-joint hand skeleton and the head pose while the hand is in view |
| glove IMUs | CyberFinger glove over BLE (wrist + knuckle sensors) | hand orientation and wrist bend when the camera cannot see the hand |
| forearm EMG | MindRove 8-channel armband | finger posture and the key postures (open, fist, point, pinch, victory) out of view |

When the camera loses the hand, the Studio keeps drawing it: the wrist position is predicted from the glove IMUs, the
orientation comes from the IMUs with a camera-taught mounting calibration, the finger posture from the EMG, and a
recognised key posture snaps the hand to its template. Every source has an on/off switch so the contribution of each
sensor can be shown on its own.

## Run

```
cd bridge
pip install -r requirements.txt -r requirements-fusion-studio.txt
python fusion_studio.py
```

Top bar:

- **▶ Start glove** — connects the glove over BLE (VR mode, as in the CyberFinger Bridge) and streams its IMUs.
- **▶ Start EMG** — starts the MindRove armband stream (`device`), or the built-in `simulator` for a demo without hardware.
- **◉ Start preview** — opens the OpenXR session and streams the headset's optical hand skeleton and head pose.
- **▲ Console** — shows the log.

The window's left column has the source switches (optical, wrist IMU, knuckle IMU, EMG, position model, gate), the view
options, the hand-out-of-view choice, the continual-learning switch and the hand-size slider. The centre shows the body
from the front and from above with the tracked arm and hand; the right column shows the raw signals: hand close-up with
per-finger curls, both IMUs, the eight EMG channels and the band accelerometer, the predicted position and its depth
trace, and the headset pose.

## A session

1. Put on the glove and the armband, start the three sources, look straight ahead and press **⌖ Set forward** so the body
   heading is locked.
2. Show the hand to the camera for a few seconds while moving it a little: this calibrates the glove IMUs against the
   camera (orientation, sensor mounting) and starts the continual learning of the EMG finger posture.
3. Show each key posture (open, fist, point, pinch, victory) to the camera for a few seconds. The recogniser teaches
   itself today's EMG for each posture and then recognises them out of view.
4. Move the hand out of the camera's view (to the side, behind the back): the hand keeps its position, orientation and
   posture from the IMUs and the EMG. Toggle the source switches to see what each sensor contributes.

## Files

- `fusion_studio.py` — the application (glove BLE, SteamVR driver link, OpenXR preview, EMG dashboard, fusion, drawing).
- `openxr_skeleton.py`, `openxr_skeleton_provider.py` — OpenXR hand-tracking source (runs the session in a helper process).
- `armband_panel.py`, `armband/` — MindRove EMG stream, filters, simulator, feature extraction.
- `gesture_skeleton.py` — EMG feature vector shared by the posture models.
- `../fusion/extrinsic.py`, `../fusion/orient_fusion.py` — camera ↔ IMU calibration and orientation fusion.
- `../fusion/gate.py`, `../fusion/occlusion.py` — occlusion gate (out of view / behind an object / self-occluded) and per-finger visibility from the joints and head pose.
- `mount_calib.py` — camera-taught IMU mounting calibration (live wrist bend out of view).
- `hybrid_position.py`, `hybrid_position_weights.npz` — out-of-view wrist-position model (forearm kinematics + learned elbow motion).
- `key_postures.py`, `key_postures_model2.npz` — key-posture recogniser (EMG covariance features, MLP, camera self-teaching).
- `posture_pose_model.npz`, `hand_prior.npz` — EMG finger-posture regressor and the anatomical hand prior.

Settings (glove options, camera field of view, IMU slots) are shared with the CyberFinger Bridge in
`%APPDATA%\CyberFingerBridge\settings.json`.
