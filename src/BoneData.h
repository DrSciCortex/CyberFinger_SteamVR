/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// BoneData.h — SteamVR hand skeleton layout (31 bones per hand)
//
// Order and hierarchy per the OpenVR driver docs ("Skeletal Input"). The aux
// bones are direct children of the root and carry the model-space transform
// of each finger's last knuckle bone.
// ═══════════════════════════════════════════════════════════════════════════

namespace cf {

enum HandSkeletonBone : int {
    eBone_Root = 0,
    eBone_Wrist,
    eBone_Thumb0,
    eBone_Thumb1,
    eBone_Thumb2,
    eBone_Thumb3,
    eBone_IndexFinger0,
    eBone_IndexFinger1,
    eBone_IndexFinger2,
    eBone_IndexFinger3,
    eBone_IndexFinger4,
    eBone_MiddleFinger0,
    eBone_MiddleFinger1,
    eBone_MiddleFinger2,
    eBone_MiddleFinger3,
    eBone_MiddleFinger4,
    eBone_RingFinger0,
    eBone_RingFinger1,
    eBone_RingFinger2,
    eBone_RingFinger3,
    eBone_RingFinger4,
    eBone_PinkyFinger0,
    eBone_PinkyFinger1,
    eBone_PinkyFinger2,
    eBone_PinkyFinger3,
    eBone_PinkyFinger4,
    eBone_Aux_Thumb,
    eBone_Aux_IndexFinger,
    eBone_Aux_MiddleFinger,
    eBone_Aux_RingFinger,
    eBone_Aux_PinkyFinger,
    eBone_Count
};

constexpr int kBoneParent[eBone_Count] = {
    -1,                                                         // Root
    eBone_Root,                                                 // Wrist
    eBone_Wrist, eBone_Thumb0, eBone_Thumb1, eBone_Thumb2,      // Thumb0..3
    eBone_Wrist, eBone_IndexFinger0, eBone_IndexFinger1, eBone_IndexFinger2, eBone_IndexFinger3,
    eBone_Wrist, eBone_MiddleFinger0, eBone_MiddleFinger1, eBone_MiddleFinger2, eBone_MiddleFinger3,
    eBone_Wrist, eBone_RingFinger0, eBone_RingFinger1, eBone_RingFinger2, eBone_RingFinger3,
    eBone_Wrist, eBone_PinkyFinger0, eBone_PinkyFinger1, eBone_PinkyFinger2, eBone_PinkyFinger3,
    eBone_Root, eBone_Root, eBone_Root, eBone_Root, eBone_Root, // Aux_*
};

// Last knuckle bone of each finger, mirrored by the aux bones.
constexpr int kAuxSource[5] = {
    eBone_Thumb2, eBone_IndexFinger3, eBone_MiddleFinger3, eBone_RingFinger3, eBone_PinkyFinger3,
};

// First bone of each finger chain (thumb has 4 bones, the others 5).
constexpr int kFingerFirstBone[5] = {
    eBone_Thumb0, eBone_IndexFinger0, eBone_MiddleFinger0, eBone_RingFinger0, eBone_PinkyFinger0,
};

} // namespace cf
