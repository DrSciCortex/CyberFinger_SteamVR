/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// cyberfinger_config.cpp
// ═══════════════════════════════════════════════════════════════════════════

#include "cyberfinger_config.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJSValue>
#include <QJsonDocument>
#include <QSaveFile>
#include <QStandardPaths>

CyberFingerConfig::CyberFingerConfig(QObject * parent) :
        QObject(parent)
{
	load();
}

// As the driver's DefaultConfigPath(): XDG_CONFIG_HOME, else ~/.config
QString CyberFingerConfig::path() const
{
	return QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation) + "/cyberfinger/wivrn.json";
}

void CyberFingerConfig::load()
{
	m_json = {};
	m_original.clear();
	m_saved = false;
	m_unreadable = false;

	QFile file(path());
	m_existed = file.exists();
	if (m_existed and file.open(QIODevice::ReadOnly))
	{
		m_original = file.readAll();
		const QJsonDocument doc = QJsonDocument::fromJson(m_original);
		if (doc.isObject())
			m_json = doc.object();
		else
			m_unreadable = true;
	}
	else if (m_existed)
	{
		m_unreadable = true;
	}
	Q_EMIT loaded();
}

QVariant CyberFingerConfig::value(const QString & key, const QVariant & fallback) const
{
	const qsizetype dot = key.indexOf('.');
	const QJsonValue v = dot < 0 ? m_json.value(key) : m_json.value(key.left(dot)).toObject().value(key.mid(dot + 1));
	if (v.isUndefined() or v.isNull())
		return fallback;
	// A value of the wrong kind is what the driver ignores too
	if (fallback.typeId() == QMetaType::Bool and not v.isBool())
		return fallback;
	if (fallback.typeId() == QMetaType::QString and not v.isString())
		return fallback;
	return v.toVariant();
}

void CyberFingerConfig::setValue(const QString & key, const QVariant & value)
{
	// A JavaScript array arrives wrapped
	const QJsonValue v = QJsonValue::fromVariant(value.metaType() == QMetaType::fromType<QJSValue>() ? value.value<QJSValue>().toVariant() : value);
	const qsizetype dot = key.indexOf('.');
	if (dot < 0)
	{
		m_json.insert(key, v);
		return;
	}
	QJsonObject inner = m_json.value(key.left(dot)).toObject();
	inner.insert(key.mid(dot + 1), v);
	m_json.insert(key.left(dot), inner);
}

void CyberFingerConfig::remove(const QStringList & keys)
{
	for (const QString & key: keys)
	{
		const qsizetype dot = key.indexOf('.');
		if (dot < 0)
		{
			m_json.remove(key);
			continue;
		}
		QJsonObject inner = m_json.value(key.left(dot)).toObject();
		inner.remove(key.mid(dot + 1));
		if (inner.isEmpty())
			m_json.remove(key.left(dot));
		else
			m_json.insert(key.left(dot), inner);
	}
}

bool CyberFingerConfig::save()
{
	if (m_unreadable)
		return false;
	if (not QDir().mkpath(QFileInfo(path()).absolutePath()))
		return false;
	QSaveFile file(path());
	if (not file.open(QIODevice::WriteOnly))
		return false;
	file.write(QJsonDocument(m_json).toJson(QJsonDocument::Indented));
	if (not file.commit())
		return false;
	m_saved = true;
	return true;
}

void CyberFingerConfig::revert()
{
	if (not m_saved)
		return;
	if (m_existed)
	{
		QSaveFile file(path());
		if (file.open(QIODevice::WriteOnly))
		{
			file.write(m_original);
			file.commit();
		}
	}
	else
	{
		QFile::remove(path());
	}
	load();
}
