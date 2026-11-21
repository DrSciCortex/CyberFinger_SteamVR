/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 * SPDX-FileCopyrightText: Valve Corporation
 *
 * SPDX-License-Identifier: GPL-3.0-only AND BSD-3-Clause
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// SkeletonSynth.h — procedural SteamVR hand skeletons and skeleton utilities
//
// Synthesize() is a port of Valve's OpenVR SDK sample "handskeletonsimulation"
// (samples/drivers/drivers/handskeletonsimulation/src/hand_simulation.cpp),
// which builds bones with the conventions SteamVR and applications expect.
// The skeleton root is /pose/raw and the wrist bone carries the raw → wrist
// offset of an Index controller.
// ═══════════════════════════════════════════════════════════════════════════

#include "BoneData.h"
#include "MathUtil.h"
#include "VrTypes.h"

namespace cf {

// curls: thumb..pinky, 0 open .. 1 closed. splays: -1 .. 1.
void SynthesizeSkeleton(bool rightHand, const float curls[5], const float splays[5],
                        vr::VRBoneTransform_t out[eBone_Count]);

// The synthesized skeleton's wrist bone (raw → wrist), identical for every pose.
Xform SynthWristBone(bool rightHand);

// Model-space (root-relative) transforms by forward kinematics.
void ModelSpace(const vr::VRBoneTransform_t bones[eBone_Count], Xform model[eBone_Count]);

// Overwrites the aux bones from the finger chains.
void FillAuxBones(vr::VRBoneTransform_t bones[eBone_Count]);

// Where a skeleton puts the wrist relative to its /pose/raw (root · wrist bone).
Xform SourceWrist(const vr::VRBoneTransform_t* bones);

// Re-roots another driver's skeleton onto our /pose/raw, whose wrist bone is ourWrist. With
// raw_ours = raw_src · srcWrist · ourWrist⁻¹ every bone keeps its world transform: the finger chains
// hang off the wrist and carry over unchanged, and the root-parented aux bones move with the root.
void ReRootBones(const vr::VRBoneTransform_t* src, const Xform& srcWrist, const Xform& ourWrist,
                 vr::VRBoneTransform_t out[eBone_Count]);

// +1 if the skeleton follows the right-hand convention, -1 for left, 0 if unclear.
// SteamVR mirrors the hands: finger bones extend along +X of their parent on the
// left hand and along -X on the right.
int SkeletonHandedness(const vr::VRBoneTransform_t* bones, uint32_t count);

// Per-finger curl estimate (0 open .. 1 closed) from joint bend angles.
void CurlsFromBones(const vr::VRBoneTransform_t bones[eBone_Count], float curls[5]);

// Hand-tracking gestures from a skeleton, for when the headset's own gesture
// values are unavailable. pinch[i]: thumb tip to index/middle/ring/pinky tip,
// 0 (>= 6 cm apart) .. 1 (<= 2 cm). grasp: mean curl of the four fingers.
struct HandGestures {
    float pinch[4] = {};
    float grasp = 0.f;
    bool  indexPoint = false;
    bool  twoFingerPoint = false;      // index and middle out, ring and pinky curled, the thumb across them
    float curls[5] = {};               // thumb..pinky, as the gestures saw them (GesturesFromBones)
    const char* twoFingerWhy = nullptr;  // why it isn't a two-finger point (TwoFingerPointShape)
};
HandGestures GesturesFromBones(const vr::VRBoneTransform_t bones[eBone_Count]);
// The two-finger point from the curls (thumb..pinky): index and middle out, ring and pinky folded, the thumb across
// them. why: the first rule that fails, else null.
bool TwoFingerPointShape(const float curls[5], const char** why = nullptr);
float PinchFromDistance(double metres);

// The palm in the skeleton's model space: its centre (the wrist and the index and pinky knuckles) and its unit normal,
// out of the palm (the way the fingers curl). right: which hand.
void PalmFrame(bool right, const vr::VRBoneTransform_t bones[eBone_Count], Vec3& centre, Vec3& normal);

// What a pinky pinch looks like, to tell a deliberate one from any thumb-to-pinky contact.
struct PinkyPinchShape {
    double palmToHeadDeg = 180;        // the palm's normal against the direction to the head (0: facing the face)
    float  curls[5] = {};              // thumb..pinky, 0 open .. 1 closed
    double thumbTo[4] = {};            // m, the thumb tip to the index..pinky tips
};
// pose: the hand pose the skeleton is relative to; head: the headset's position (same space), or null if unknown.
PinkyPinchShape PinkyShape(bool right, const vr::VRBoneTransform_t bones[eBone_Count], const Xform& pose,
                           const Vec3* head);
// A deliberate pinky pinch: the palm toward the face, the index and middle fingers relaxed and fairly straight, and
// the pinky alone at the thumb (a thumb on the last two or three fingers is a grip, not a pinch). why: what failed.
bool PinkyPinchMeant(const PinkyPinchShape& s, const char** why = nullptr);

} // namespace cf
