/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// dashboard_settings_test.cpp — the dashboard's CyberFinger settings section,
// without the dashboard
//
//   QT_QPA_PLATFORM=offscreen dashboard_settings_test <tests dir> <scratch dir> [screenshot.png]
//
// Loads CyberFingerSettings.qml with the real CyberFingerConfig on a settings
// file in the scratch folder: the defaults show without a file, a change is
// written as the driver reads it (CfConfig.cpp), Cancel puts the file back,
// Reset drops the page's keys and keeps the others, a file that isn't plain
// JSON is left alone. Any QML warning fails the test.
// ═══════════════════════════════════════════════════════════════════════════

#include <KLocalizedQmlContext>
#include <KLocalizedString>
#include <QApplication>
#include <QDir>
#include <QFile>
#include <QImage>
#include <QJSValue>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQmlExpression>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QTest>
#include <cstdio>
#include <fstream>
#include <sstream>
#include "CfConfig.h"
#include "cyberfinger_config.h"

static int g_failures = 0;
static QStringList g_warnings;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static QJsonObject ReadJson(const QString& path) {
    QFile f(path);
    return f.open(QIODevice::ReadOnly) ? QJsonDocument::fromJson(f.readAll()).object() : QJsonObject{};
}

// The settings file as the driver takes it.
static bool DriverReads(const QString& path, cf::Config& cfg, std::string& warnings) {
    std::ifstream f(path.toStdString());
    std::stringstream text;
    text << f.rdbuf();
    return cf::ParseConfig(text.str(), cfg, warnings);
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::printf("usage: dashboard_settings_test <tests dir> <scratch dir> [screenshot.png]\n");
        return 2;
    }
    const QString tests = argv[1], scratch = QString(argv[2]) + "/dashboard_settings_scratch";
    QDir(scratch).removeRecursively();
    QDir().mkpath(scratch);
    qputenv("XDG_CONFIG_HOME", scratch.toUtf8());
    const QString path = scratch + "/cyberfinger/wivrn.json";

    qInstallMessageHandler([](QtMsgType type, const QMessageLogContext&, const QString& msg) {
        if (type != QtDebugMsg && type != QtInfoMsg) g_warnings << msg;
        std::fprintf(stderr, "%s\n", qPrintable(msg));
    });
    QApplication app(argc, argv);
    KLocalizedString::setApplicationDomain("wivrn-dashboard");
    QQuickStyle::setStyle(QStringLiteral("org.kde.desktop"));
    QQmlApplicationEngine engine;
    KLocalization::setupLocalizedContext(&engine);
    auto* config = new CyberFingerConfig(&engine);
    qmlRegisterSingletonInstance("io.github.wivrn.wivrn", 1, 0, "CyberFingerConfig", config);
    engine.load(QUrl::fromLocalFile(tests + "/DashboardSettingsTest.qml"));
    CHECK(!engine.rootObjects().isEmpty());
    if (engine.rootObjects().isEmpty()) return 1;
    QObject* root = engine.rootObjects().first();
    auto* section = root->property("section").value<QObject*>();
    CHECK(section);
    if (!section) return 1;
    // JavaScript inside the section's own file, where its controls' ids are: the context of something declared
    // there (the section's own context is the test page's).
    QQmlContext* inside = nullptr;
    for (QObject* child : section->children())
        if (QQmlContext* c = qmlContext(child); c && c != qmlContext(section) && !inside) inside = c;
    CHECK(inside);
    if (!inside) return 1;
    auto js = [&](const QString& code) {
        QQmlExpression e(inside, section, code);
        bool undefined = false;
        const QVariant v = e.evaluate(&undefined);
        if (e.hasError()) {
            std::printf("FAIL js: %s: %s\n", qPrintable(code), qPrintable(e.error().toString()));
            ++g_failures;
        }
        return v;
    };
    QMetaObject::invokeMethod(section, "load");
    QTest::qWait(200);

    // No file: the driver's defaults show, and nothing is written by looking.
    CHECK(config->path() == path && !config->unreadable());
    CHECK(js("active.checked && imu_fusion.checked && grab_tap_to_hold.checked && flux.checked").toBool());
    CHECK(js("profile.currentValue").toString() == "touch");
    CHECK(js("black_hold.value").toInt() == 800 && js("grab_tap.value").toInt() == 200);
    CHECK(js("button_a.editText").toString() == "STSEL" && js("button_menu.editText").toString() == "PINK");
    CHECK(js("flux_a_hold.value").toInt() == 1 && js("flux_pink_right.value").toInt() == 42);
    QMetaObject::invokeMethod(section, "save");
    CHECK(!QFile::exists(path));
    {   // … and they are the driver's own defaults
        const cf::Config d;
        CHECK(js("occlusion_hold.value").toDouble() == d.occlusionHold * 1e3);
        CHECK(js("release_after.value").toDouble() == d.releaseAfter * 1e3);
        CHECK(js("grab_tap.value").toDouble() == d.grabTapTime * 1e3 && js("black_hold.value").toDouble() == d.blackHoldTime * 1e3);
        CHECK(js("rotation_prediction.value").toDouble() == d.rotationPrediction);
        CHECK(js("flux_c.value").toInt() == d.fluxC && js("flux_d.value").toInt() == d.fluxD && js("flux_e.value").toInt() == d.fluxE);
        CHECK(js("flux_two_finger_point.value").toInt() == d.fluxTwoFingerPoint &&
              js("flux_pinky_pinch.value").toInt() == d.fluxPinkyPinch && js("flux_index_point.value").toInt() == d.fluxIndexPoint);
        CHECK(js("cf.defaults.flux_port").toInt() == d.fluxPort);
    }

    // Changes are written shortly after, and the driver reads them as meant.
    js("imu_fusion.checked = false; black_hold.value = 0; grab_tap.value = 350; rotation_prediction.value = 0.25;"
       "profile.currentIndex = profile.indexOfValue('index'); button_b.show('menu|d'); button_system.show('PINK');"
       "offset_x.value = 12; offset_z.value = -30; angle_y.value = 45; flux_c.value = 0; flux_pink_right.value = 7;"
       "cf.store()");
    CHECK(!QFile::exists(path));                         // not before the short delay
    QTest::qWait(400);
    CHECK(QFile::exists(path));
    {
        cf::Config c;
        std::string warnings;
        CHECK(DriverReads(path, c, warnings) && warnings.empty());
        CHECK(c.active && !c.imuFusion && c.profile == "index" && c.blackHoldTime == 0);
        CHECK(std::abs(c.grabTapTime - 0.35) < 1e-9 && std::abs(c.rotationPrediction - 0.25) < 1e-9);
        CHECK(c.maskB == (cf::kBtnMenu | cf::kBtnD) && c.maskSystem == cf::kMaskPink && c.maskA == cf::kBtnStartSelect);
        CHECK(std::abs(c.gripOffset.p.x - 0.012) < 1e-9 && std::abs(c.gripOffset.p.z + 0.030) < 1e-9);
        CHECK(std::abs(c.gripOffset.q.y - std::sin(cf::DegToRad(45) / 2)) < 1e-6);
        CHECK(c.fluxPort == 42042 && c.fluxC == 0 && c.fluxPinkRight == 7 && c.fluxD == 5);
    }
    // FluxActions off is port 0; on again, the port it had.
    js("flux.checked = false; cf.store(); cf.save()");
    CHECK(ReadJson(path).value("flux_port").toInt(-1) == 0);
    js("flux.checked = true; cf.store(); cf.save()");
    CHECK(ReadJson(path).value("flux_port").toInt(-1) == 42042);

    // Cancel: no file before, none after; the page shows the defaults again on its next load.
    QMetaObject::invokeMethod(section, "revert");
    CHECK(!QFile::exists(path));
    QMetaObject::invokeMethod(section, "load");
    CHECK(js("imu_fusion.checked").toBool() && js("black_hold.value").toInt() == 800);

    // A file with keys of its own: kept through a change, a Cancel (byte for byte) and a Reset.
    const QByteArray mine = "{\"handtracking_udp_port\": 37015, \"black_hold_ms\": 500, \"flux_actions\": {\"c\": 9}, "
                            "\"my_note\": \"kept\"}\n";
    {
        QFile f(path);
        CHECK(f.open(QIODevice::WriteOnly));
        f.write(mine);
    }
    QMetaObject::invokeMethod(section, "load");
    CHECK(js("black_hold.value").toInt() == 500 && js("flux_c.value").toInt() == 9 && js("flux_d.value").toInt() == 5);
    js("active.checked = false; cf.store(); cf.save()");
    QJsonObject j = ReadJson(path);
    CHECK(j.value("handtracking_udp_port").toInt() == 37015 && j.value("my_note").toString() == "kept");
    CHECK(j.value("active").isBool() && !j.value("active").toBool() && j.value("black_hold_ms").toInt() == 500);
    QMetaObject::invokeMethod(section, "revert");
    {
        QFile f(path);
        CHECK(f.open(QIODevice::ReadOnly) && f.readAll() == mine);
    }
    CHECK(js("active.checked").toBool() == false);       // the controls are the page's to reload
    QMetaObject::invokeMethod(section, "load");
    CHECK(js("active.checked").toBool() && js("black_hold.value").toInt() == 500);
    QMetaObject::invokeMethod(section, "restoreDefaults");
    j = ReadJson(path);
    CHECK(j.value("handtracking_udp_port").toInt() == 37015 && j.value("my_note").toString() == "kept");
    CHECK(!j.contains("black_hold_ms") && !j.contains("flux_actions") && !j.contains("active"));
    CHECK(js("black_hold.value").toInt() == 800 && js("flux_c.value").toInt() == 3);

    // Not plain JSON (the driver allows comments): shown as unreadable, never overwritten.
    const QByteArray commented = "{\n  // mine\n  \"active\": false\n}\n";
    {
        QFile f(path);
        CHECK(f.open(QIODevice::WriteOnly | QIODevice::Truncate));
        f.write(commented);
    }
    QMetaObject::invokeMethod(section, "load");
    CHECK(config->unreadable());
    js("black_hold.value = 100; cf.store(); cf.save(); cf.restoreDefaults()");
    QTest::qWait(300);
    {
        QFile f(path);
        CHECK(f.open(QIODevice::ReadOnly) && f.readAll() == commented);
    }
    QFile::remove(path);
    QMetaObject::invokeMethod(section, "load");
    CHECK(!config->unreadable());

    if (argc > 3) {
        QTest::qWait(300);
        if (auto* window = qobject_cast<QQuickWindow*>(root)) window->grabWindow().save(argv[3]);
    }
    for (const QString& w : g_warnings) std::printf("QML warning: %s\n", qPrintable(w));
    CHECK(g_warnings.isEmpty());
    if (g_failures) {
        std::printf("%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("dashboard settings test passed\n");
    return 0;
}
