/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */

// ═══════════════════════════════════════════════════════════════════════════
// handtracking_bridge.cpp
//
// Reads hand tracking data from SteamVR tracked devices (Quest via Steam
// Link, Virtual Desktop, etc.) and sends it over UDP to the CyberFinger
// driver using the HandTrackingPacket wire format.
//
// Uses tracked device poses (GetDeviceToAbsoluteTrackingPose) rather than
// the action system, since skeleton actions aren't available to background
// apps when streaming from Quest.
//
// Usage:
//   handtracking_bridge [--port 27015]
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
static int g_htSent[2] = {0, 0};

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

// ── Matrix to quaternion ──────────────────────────────────────────────────

static void MatToQuat(const float m[3][4], float& qw, float& qx, float& qy, float& qz) {
    float trace = m[0][0] + m[1][1] + m[2][2];
    if (trace > 0) {
        float s = 0.5f / std::sqrt(trace + 1.0f);
        qw = 0.25f / s;
        qx = (m[2][1] - m[1][2]) * s;
        qy = (m[0][2] - m[2][0]) * s;
        qz = (m[1][0] - m[0][1]) * s;
    } else if (m[0][0] > m[1][1] && m[0][0] > m[2][2]) {
        float s = 2.0f * std::sqrt(1.0f + m[0][0] - m[1][1] - m[2][2]);
        qw = (m[2][1] - m[1][2]) / s;
        qx = 0.25f * s;
        qy = (m[0][1] + m[1][0]) / s;
        qz = (m[0][2] + m[2][0]) / s;
    } else if (m[1][1] > m[2][2]) {
        float s = 2.0f * std::sqrt(1.0f + m[1][1] - m[0][0] - m[2][2]);
        qw = (m[0][2] - m[2][0]) / s;
        qx = (m[0][1] + m[1][0]) / s;
        qy = 0.25f * s;
        qz = (m[1][2] + m[2][1]) / s;
    } else {
        float s = 2.0f * std::sqrt(1.0f + m[2][2] - m[0][0] - m[1][1]);
        qw = (m[1][0] - m[0][1]) / s;
        qx = (m[0][2] + m[2][0]) / s;
        qy = (m[1][2] + m[2][1]) / s;
        qz = 0.25f * s;
    }
}

// ── Find hand controllers by role ─────────────────────────────────────────

static vr::TrackedDeviceIndex_t FindHandTracker(int hand) {
    // hand: 0=left, 1=right
    const char* leftPatterns[] = {"Hand_Left", "hand_left", "Left Hand", nullptr};
    const char* rightPatterns[] = {"Hand_Right", "hand_right", "Right Hand", nullptr};
    const char** patterns = (hand == 0) ? leftPatterns : rightPatterns;

    for (vr::TrackedDeviceIndex_t i = 0; i < vr::k_unMaxTrackedDeviceCount; ++i) {
        auto cls = vr::VRSystem()->GetTrackedDeviceClass(i);
        if (cls != vr::TrackedDeviceClass_Controller)
            continue;

        char serial[256] = {};
        vr::VRSystem()->GetStringTrackedDeviceProperty(i, vr::Prop_SerialNumber_String, serial, sizeof(serial));

        // Skip our own controllers
        if (strstr(serial, "CYBERFINGER") != nullptr)
            continue;

        // Check serial against patterns
        for (const char** p = patterns; *p; ++p) {
            if (strstr(serial, *p) != nullptr)
                return i;
        }

        // Also check model number
        char model[256] = {};
        vr::VRSystem()->GetStringTrackedDeviceProperty(i, vr::Prop_ModelNumber_String, model, sizeof(model));
        for (const char** p = patterns; *p; ++p) {
            if (strstr(model, *p) != nullptr)
                return i;
        }
    }
    return vr::k_unTrackedDeviceIndexInvalid;
}

// ── Poll tracked device poses ─────────────────────────────────────────────

static int g_pollCount = 0;
static int g_noController[2] = {0, 0};

static void PollTrackedDeviceHands() {
    g_pollCount++;

    // Get all poses at once in standing (absolute) space
    vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
    vr::VRSystem()->GetDeviceToAbsoluteTrackingPose(
        vr::TrackingUniverseStanding, 0.f, poses, vr::k_unMaxTrackedDeviceCount);

    for (int hand = 0; hand < 2; ++hand) {
        const char* hn = (hand == 0) ? "L" : "R";

        vr::TrackedDeviceIndex_t devIdx = FindHandTracker(hand);
        if (devIdx == vr::k_unTrackedDeviceIndexInvalid) {
            g_noController[hand]++;
            if (g_noController[hand] == 1 || g_noController[hand] % 1000 == 0)
                printf("  [%s] No controller found (skip #%d)\n", hn, g_noController[hand]);
            continue;
        }
        g_noController[hand] = 0;

        if (!poses[devIdx].bPoseIsValid)
            continue;

        // Extract absolute position and rotation from the 3x4 matrix
        const auto& mat = poses[devIdx].mDeviceToAbsoluteTracking.m;
        float px = mat[0][3], py = mat[1][3], pz = mat[2][3];
        float qw, qx, qy, qz;
        MatToQuat(mat, qw, qx, qy, qz);

        // Build packet — positions in absolute standing space
        HandTrackingPacket pkt{};
        pkt.magic = kMagic;
        pkt.version = 1;
        pkt.hand = (uint8_t)hand;
        pkt.confidence = 200;
        pkt.pos[0] = px;
        pkt.pos[1] = py;
        pkt.pos[2] = pz;
        pkt.quat[0] = qw;
        pkt.quat[1] = qx;
        pkt.quat[2] = qy;
        pkt.quat[3] = qz;

        // Identity bones (pose-only for now, skeleton TBD)
        for (int i = 0; i < kNumBones; ++i) {
            pkt.bones[i][0] = 0.f;
            pkt.bones[i][1] = 0.f;
            pkt.bones[i][2] = 0.f;
            pkt.bones[i][3] = 1.f; // quat w = identity
            pkt.bones[i][4] = 0.f;
            pkt.bones[i][5] = 0.f;
            pkt.bones[i][6] = 0.f;
        }

        // Root bone gets device pose
        pkt.bones[0][0] = px;
        pkt.bones[0][1] = py;
        pkt.bones[0][2] = pz;
        pkt.bones[0][3] = qw;
        pkt.bones[0][4] = qx;
        pkt.bones[0][5] = qy;
        pkt.bones[0][6] = qz;

        // Curls default to 0 (open hand)
        for (int i = 0; i < 5; ++i)
            pkt.curls[i] = 0.f;

        SendPacket(pkt);

        g_htSent[hand]++;
        if (g_htSent[hand] == 1) {
            printf("  [%s] FIRST HT packet! pos=(%.3f,%.3f,%.3f) dev=%d\n",
                   hn, px, py, pz, devIdx);
        } else if (g_htSent[hand] % 500 == 0) {
            printf("  [%s] %d HT packets, pos=(%.3f,%.3f,%.3f)\n",
                   hn, g_htSent[hand], px, py, pz);
        }
    }
}

// ── Main ───────────────────────────────────────────────────────────────────

int main(int argc, char* argv[]) {
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "--port" && i + 1 < argc)
            g_port = std::atoi(argv[++i]);
    }

    printf("=== CyberFinger Hand Tracking Bridge ===\n");
    printf("UDP port: %d\n", g_port);

    vr::EVRInitError vrErr;
    vr::VR_Init(&vrErr, vr::VRApplication_Background);
    if (vrErr != vr::VRInitError_None) {
        printf("Failed to init SteamVR: %s\n",
               vr::VR_GetVRInitErrorAsEnglishDescription(vrErr));
        printf("Make sure SteamVR is running.\n");
        return 1;
    }
    printf("SteamVR connected.\n");

    InitSocket();
    printf("UDP socket ready -> 127.0.0.1:%d\n", g_port);

    // Enumerate initial devices
    printf("\nTracked devices:\n");
    for (vr::TrackedDeviceIndex_t i = 0; i < vr::k_unMaxTrackedDeviceCount; ++i) {
        auto cls = vr::VRSystem()->GetTrackedDeviceClass(i);
        if (cls == vr::TrackedDeviceClass_Invalid) continue;

        char serial[256] = {}, model[256] = {};
        vr::VRSystem()->GetStringTrackedDeviceProperty(i, vr::Prop_SerialNumber_String, serial, sizeof(serial));
        vr::VRSystem()->GetStringTrackedDeviceProperty(i, vr::Prop_ModelNumber_String, model, sizeof(model));

        auto role = vr::VRSystem()->GetControllerRoleForTrackedDeviceIndex(i);
        const char* roleStr = "";
        if (role == vr::TrackedControllerRole_LeftHand) roleStr = " [LEFT]";
        else if (role == vr::TrackedControllerRole_RightHand) roleStr = " [RIGHT]";

        const char* clsStr = "?";
        if (cls == vr::TrackedDeviceClass_HMD) clsStr = "HMD";
        else if (cls == vr::TrackedDeviceClass_Controller) clsStr = "Controller";
        else if (cls == vr::TrackedDeviceClass_GenericTracker) clsStr = "Tracker";
        else if (cls == vr::TrackedDeviceClass_TrackingReference) clsStr = "TrackRef";

        printf("  [%d] %s  %s  %s%s\n", i, clsStr, serial, model, roleStr);
    }

    printf("\nRunning... Press Ctrl+C to stop.\n\n");

    while (true) {
        vr::VREvent_t event;
        while (vr::VRSystem()->PollNextEvent(&event, sizeof(event))) {
            if (event.eventType == vr::VREvent_Quit) {
                printf("SteamVR quit event\n");
                goto cleanup;
            }
            if (event.eventType == vr::VREvent_TrackedDeviceActivated)
                printf("  Device %d activated\n", event.trackedDeviceIndex);
            else if (event.eventType == vr::VREvent_TrackedDeviceDeactivated)
                printf("  Device %d deactivated\n", event.trackedDeviceIndex);
            else if (event.eventType == vr::VREvent_TrackedDeviceRoleChanged)
                printf("  Device %d role changed\n", event.trackedDeviceIndex);
        }

        PollTrackedDeviceHands();
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
    printf("Shut down. Sent L:%d R:%d packets.\n", g_htSent[0], g_htSent[1]);
    return 0;
}
