"""Translations for texts the backend renders itself (emails). English is the default."""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "test.subject": "Test email from {app}",
        "test.title": "Your mail server works",
        "test.body": "This is a test email sent by {app}. If you can read this, the SMTP settings are correct.",
        "test.details": "Sent via {host}:{port} using {security}.",
        "reset.subject": "Reset your {app} password",
        "reset.title": "Reset your password",
        "reset.body": "Someone (hopefully you) asked to reset the password for {username}. "
        "The link is valid for 60 minutes.",
        "reset.button": "Choose a new password",
        "reset.ignore": "Didn't ask for this? You can safely ignore this email.",
        "backup.failed.subject": "Backup failed: {target}",
        "backup.failed.title": "A backup failed",
        "backup.failed.body": "The {kind} backup to {target} failed at {time}.",
        "backup.ok.subject": "Backup completed: {target}",
        "backup.ok.title": "Backup completed",
        "backup.ok.body": "The {kind} backup to {target} completed at {time} ({size}).",
        "update.subject": "{app} updated to {version}",
        "update.title": "Update installed",
        "update.body": "{app} was updated from {old} to {version} on the {channel} channel.",
        "footer": "Sent by {app}.",
    },
    "nl": {
        "test.subject": "Testmail van {app}",
        "test.title": "Je mailserver werkt",
        "test.body": "Dit is een testmail van {app}. Kun je dit lezen, dan kloppen de SMTP-instellingen.",
        "test.details": "Verstuurd via {host}:{port} met {security}.",
        "reset.subject": "Stel je wachtwoord voor {app} opnieuw in",
        "reset.title": "Nieuw wachtwoord instellen",
        "reset.body": "Iemand (hopelijk jij) heeft gevraagd het wachtwoord van {username} opnieuw in te stellen. "
        "De link is 60 minuten geldig.",
        "reset.button": "Kies een nieuw wachtwoord",
        "reset.ignore": "Niet aangevraagd? Dan kun je deze mail negeren.",
        "backup.failed.subject": "Backup mislukt: {target}",
        "backup.failed.title": "Een backup is mislukt",
        "backup.failed.body": "De {kind} backup naar {target} is mislukt om {time}.",
        "backup.ok.subject": "Backup gelukt: {target}",
        "backup.ok.title": "Backup gelukt",
        "backup.ok.body": "De {kind} backup naar {target} is gelukt om {time} ({size}).",
        "update.subject": "{app} bijgewerkt naar {version}",
        "update.title": "Update geïnstalleerd",
        "update.body": "{app} is bijgewerkt van {old} naar {version} op het {channel}-kanaal.",
        "footer": "Verstuurd door {app}.",
    },
}

LANGUAGES = ("en", "nl")


def t(lang: str, key: str, **kwargs: object) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    text = table.get(key) or STRINGS["en"].get(key) or key
    return text.format(**kwargs)
