/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
#pragma once
// ═══════════════════════════════════════════════════════════════════════════
// cyberfinger_config.h — the CyberFinger driver's settings file, for the
// WiVRn dashboard's settings page (CyberFingerSettings.qml)
//
// ~/.config/cyberfinger/wivrn.json (driver/CfConfig.h has the keys). The
// driver reads the file again whenever it changes, so a save here is in
// effect within a second, WiVRn running or not. Keys the page doesn't show
// (the UDP ports) are kept as they are.
// ═══════════════════════════════════════════════════════════════════════════

#include <QByteArray>
#include <QJsonObject>
#include <QObject>
#include <QString>
#include <QVariant>
#include <qqmlintegration.h>

class CyberFingerConfig : public QObject
{
	Q_OBJECT
	QML_NAMED_ELEMENT(CyberFingerConfig)
	QML_SINGLETON

	// The file exists but isn't a JSON object (hand-edited, with comments say): the page then leaves it alone
	Q_PROPERTY(bool unreadable READ unreadable NOTIFY loaded)
	Q_PROPERTY(QString path READ path CONSTANT)

public:
	explicit CyberFingerConfig(QObject * parent = nullptr);

	bool unreadable() const
	{
		return m_unreadable;
	}
	QString path() const;

	// Reads the file, and remembers it as it is for revert()
	Q_INVOKABLE void load();
	// A key's value, or fallback if the file doesn't have it. "flux_actions.c": c inside the flux_actions object
	Q_INVOKABLE QVariant value(const QString & key, const QVariant & fallback) const;
	Q_INVOKABLE void setValue(const QString & key, const QVariant & value);
	// Drops these keys, so the driver's defaults apply again
	Q_INVOKABLE void remove(const QStringList & keys);
	// Writes the file; false if it could not be written
	Q_INVOKABLE bool save();
	// Puts the file back as load() found it, if a save changed it
	Q_INVOKABLE void revert();

Q_SIGNALS:
	void loaded();

private:
	QJsonObject m_json;
	QByteArray m_original; // the file as loaded
	bool m_existed = false;
	bool m_saved = false; // since load()
	bool m_unreadable = false;
};
