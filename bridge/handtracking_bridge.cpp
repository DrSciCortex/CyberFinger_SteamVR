/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */

// ═══════════════════════════════════════════════════════════════════════════
// handtracking_bridge.cpp
//
// Standalone companion application that reads hand tracking data from:
//   1. SteamVR's built-in hand tracking (Quest via Steam Link, etc.)
//   2. Ultraleap (Leap Motion) Gemini SDK
//
// And sends it over UDP to the merged_controller driver using the
// HandTrackingPacket wire format.
//
// Build:
//   cl handtracking_bridge.cpp /I <openvr>/headers /link openvr_api.lib ws2_32.lib
//   g++ handtracking_bridge.cpp -I<openvr>/headers -lopenvr_api -lpthread -o bridge
//
// Usage:
//   handtracking_bridge [--port 27015] [--source auto|steamvr|ultraleap]
// ═══════════════════════════════════════════════════════════════════════════

#ifdef _WIN32
#  define NOMINMAX
#  define WIN32_LEAN_AND_MEAN
#endif

#include <openvr.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cmath>
#include <chrono>
#include <thread>
#include <string>

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  pragma comment(lib, "ws2_32.lib")
   typedef int socklen_t;
#else
#  include <sys/socket.h>
#  include <netinet/in.h>
#  include <arpa/inet.h>
#  include <unistd.h>
#endif

// ── Wire protocol (must match driver's HandTrackingPacket) ─────────────────

static constexpr int kNumBones = 31;

#pragma pack(push, 1)
struct HandTrackingPacket {
    uint32_t magic;
    uint8_t  version;
    uint8_t  hand;
    uint8_t  confidence;
    uint8_t  reserved;
    float    pos[3];
    float    quat[4];
    float    bones[kNumBones][7];
    float    curls[5];
};
#pragma pack(pop)

static constexpr uint32_t kMagic = 0x4B535448;

// ── Globals ────────────────────────────────────────────────────────────────

static int g_port = 27015;
static int g_sock = -1;
static struct sockaddr_in g_dest;

static void InitSocket() {
#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
    g_sock = (int)socket(AF_INET, SOCK_DGRAM, 0);
    memset(&g_dest, 0, sizeof(g_dest));
    g_dest.sin_family = AF_INET;
    g_dest.sin_port = htons(g_port);
    inet_pton(AF_INET, "127.0.0.1", &g_dest.sin_addr);
}

static void SendPacket(const HandTrackingPacket& pkt) {
    sendto(g_sock, (const char*)&pkt, sizeof(pkt), 0,
           (struct sockaddr*)&g_dest, sizeof(g_dest));
}

// ── SteamVR Hand Tracking Source ───────────────────────────────────────────

static vr::VRActionHandle_t g_skeletonLeft  = vr::k_ulInvalidActionHandle;
static vr::VRActionHandle_t g_skeletonRight = vr::k_ulInvalidActionHandle;
static vr::VRActionSetHandle_t g_actionSet  = vr::k_ulInvalidActionSetHandle;

// We use SteamVR's input system to read hand tracking skeletons.
// This requires an action manifest.

static const char* kActionManifest = R"({
    "actions": [
        {
            "name": "/actions/handtracking/in/skeleton_left",
            "type": "skeleton",
            "skeleton": "/skeleton/hand/left"
        },
        {
            "name": "/actions/handtracking/in/skeleton_right",
            "type": "skeleton",
            "skeleton": "/skeleton/hand/right"
        }
    ],
    "action_sets": [
        {
            "name": "/actions/handtracking",
            "usage": "leftright"
        }
    ],
    "default_bindings": []
})";

static bool InitSteamVRInput() {
    // Write temporary action manifest
    FILE* f = fopen("bridge_actions.json", "w");
    if (!f) return false;
    fprintf(f, "%s", kActionManifest);
    fclose(f);

    // Get full path
    char path[1024];
#ifdef _WIN32
    _fullpath(path, "bridge_actions.json", sizeof(path));
#else
    realpath("bridge_actions.json", path);
#endif

    vr::EVRInputError err = vr::VRInput()->SetActionManifestPath(path);
    if (err != vr::VRInputError_None) {
        printf("Failed to set action manifest: %d\n", err);
        return false;
    }

    vr::VRInput()->GetActionHandle("/actions/handtracking/in/skeleton_left", &g_skeletonLeft);
    vr::VRInput()->GetActionHandle("/actions/handtracking/in/skeleton_right", &g_skeletonRight);
    vr::VRInput()->GetActionSetHandle("/actions/handtracking", &g_actionSet);

    return true;
}

static float ComputeFingerCurl(const vr::VRBoneTransform_t* bones, int startBone, int numJoints) {
    // Average the rotation angle across joints to get curl value
    float totalCurl = 0.f;
    int count = 0;
    for (int i = 1; i < numJoints; ++i) {
        const auto& q = bones[startBone + i].orientation;
        // The curl is primarily around the X axis
        float angle = 2.f * std::acos(std::min(1.f, std::abs(q.w)));
        totalCurl += angle / 1.5708f; // normalize to 0..1 (π/2 = full curl)
        ++count;
    }
    return count > 0 ? std::min(1.f, totalCurl / count) : 0.f;
}

static void PollSteamVRHands() {
    vr::VRActiveActionSet_t activeSet{};
    activeSet.ulActionSet = g_actionSet;
    vr::VRInput()->UpdateActionState(&activeSet, sizeof(activeSet), 1);

    for (int hand = 0; hand < 2; ++hand) {
        vr::VRActionHandle_t skelAction = (hand == 0) ? g_skeletonLeft : g_skeletonRight;

        vr::InputSkeletalActionData_t skelData{};
        skelData.bActive = false;

        vr::VRInput()->GetSkeletalActionData(skelAction, &skelData, sizeof(skelData));
        if (!skelData.bActive) continue;

        vr::VRBoneTransform_t boneTransforms[kNumBones];
        vr::EVRInputError err = vr::VRInput()->GetSkeletalBoneData(
            skelAction,
            vr::VRSkeletalTransformSpace_Parent,
            vr::VRSkeletalMotionRange_WithoutController,
            boneTransforms, kNumBones
        );

        if (err != vr::VRInputError_None) continue;

        // Get the controller pose for hand position
        vr::InputPoseActionData_t poseData{};
        // For hand tracking, the bone root gives us the hand position
        // Use the wrist bone (bone 1) transform relative to tracking space

        HandTrackingPacket pkt{};
        pkt.magic = kMagic;
        pkt.version = 1;
        pkt.hand = (uint8_t)hand;
        pkt.confidence = 200; // Good confidence from SteamVR tracking

        // Root bone position (in parent space, which for root = tracking space)
        pkt.pos[0] = boneTransforms[0].position.v[0];
        pkt.pos[1] = boneTransforms[0].position.v[1];
        pkt.pos[2] = boneTransforms[0].position.v[2];

        pkt.quat[0] = boneTransforms[0].orientation.w;
        pkt.quat[1] = boneTransforms[0].orientation.x;
        pkt.quat[2] = boneTransforms[0].orientation.y;
        pkt.quat[3] = boneTransforms[0].orientation.z;

        // Pack all bone transforms
        for (int i = 0; i < kNumBones; ++i) {
            pkt.bones[i][0] = boneTransforms[i].position.v[0];
            pkt.bones[i][1] = boneTransforms[i].position.v[1];
            pkt.bones[i][2] = boneTransforms[i].position.v[2];
            pkt.bones[i][3] = boneTransforms[i].orientation.w;
            pkt.bones[i][4] = boneTransforms[i].orientation.x;
            pkt.bones[i][5] = boneTransforms[i].orientation.y;
            pkt.bones[i][6] = boneTransforms[i].orientation.z;
        }

        // Compute finger curl values
        pkt.curls[0] = ComputeFingerCurl(boneTransforms, 2, 4);  // thumb
        pkt.curls[1] = ComputeFingerCurl(boneTransforms, 6, 5);  // index
        pkt.curls[2] = ComputeFingerCurl(boneTransforms, 11, 5); // middle
        pkt.curls[3] = ComputeFingerCurl(boneTransforms, 16, 5); // ring
        pkt.curls[4] = ComputeFingerCurl(boneTransforms, 21, 5); // pinky

        SendPacket(pkt);
    }
}

// ── Main ───────────────────────────────────────────────────────────────────

int main(int argc, char* argv[]) {
    std::string source = "steamvr";

    // Parse args
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "--port" && i + 1 < argc)
            g_port = std::atoi(argv[++i]);
        else if (std::string(argv[i]) == "--source" && i + 1 < argc)
            source = argv[++i];
    }

    printf("═══ Merged Controller Hand Tracking Bridge ═══\n");
    printf("Source: %s | UDP port: %d\n", source.c_str(), g_port);

    // Initialize SteamVR as an overlay/background app
    vr::EVRInitError vrErr;
    vr::VR_Init(&vrErr, vr::VRApplication_Background);
    if (vrErr != vr::VRInitError_None) {
        printf("Failed to init SteamVR: %s\n",
               vr::VR_GetVRInitErrorAsEnglishDescription(vrErr));
        return 1;
    }

    InitSocket();

    if (source == "steamvr" || source == "auto") {
        if (!InitSteamVRInput()) {
            printf("Failed to init SteamVR input\n");
            vr::VR_Shutdown();
            return 1;
        }
    }

    printf("Running... Press Ctrl+C to stop.\n");

    // Main loop
    while (true) {
        vr::VREvent_t event;
        while (vr::VRSystem()->PollNextEvent(&event, sizeof(event))) {
            if (event.eventType == vr::VREvent_Quit) {
                printf("SteamVR quit event received\n");
                goto cleanup;
            }
        }

        if (source == "steamvr" || source == "auto") {
            PollSteamVRHands();
        }

        // ~90 Hz to match typical VR frame rate
        std::this_thread::sleep_for(std::chrono::milliseconds(11));
    }

cleanup:
    vr::VR_Shutdown();

#ifdef _WIN32
    closesocket(g_sock);
    WSACleanup();
#else
    close(g_sock);
#endif

    printf("Bridge shut down.\n");
    return 0;
}
