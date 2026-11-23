"""Built-in runbook templates: a starting point that an admin copies and adapts (Runbooks > New from
template). Names and texts are in English and Dutch; the copy takes the language of the admin."""

from __future__ import annotations

import copy
from typing import Any

T = dict[str, Any]

TEMPLATES: list[T] = [
    {
        "key": "patch_server",
        "category": "maintenance",
        "name": {"en": "Patch a server", "nl": "Server patchen"},
        "description": {
            "en": "Health check, install all pending updates (approval), reboot when you approve it, wait and check "
                  "again. Updates and the reboot wait for the customer's maintenance window.",
            "nl": "Gezondheidscheck, alle openstaande updates installeren (goedkeuring), herstarten als je dat "
                  "goedkeurt, wachten en opnieuw controleren. Updates en herstart wachten op het onderhoudsvenster "
                  "van de klant.",
        },
        "params": [
            {"name": "server", "label": {"en": "Server (exact name)", "nl": "Server (exacte naam)"}, "required": True},
        ],
        "steps": [
            {"id": "check_before", "kind": "tool", "tool": "diagnose_server", "args": {"server": "{{params.server}}"},
             "title": {"en": "Health check before", "nl": "Gezondheidscheck vooraf"}},
            {"id": "refresh", "kind": "tool", "tool": "refresh_node_updates", "args": {"node": "{{params.server}}"},
             "title": {"en": "Refresh the update list", "nl": "Updatelijst verversen"}},
            {"id": "install", "kind": "tool", "tool": "install_node_updates", "approval": "always", "wait_for_job": True,
             "args": {"node": "{{params.server}}", "confirm": "{{params.server}}"},
             "title": {"en": "Install updates", "nl": "Updates installeren"}},
            {"id": "reboot", "kind": "tool", "tool": "reboot_node", "approval": "always", "optional": True,
             "args": {"node": "{{params.server}}", "confirm": "{{params.server}}"},
             "title": {"en": "Reboot (reject to skip)", "nl": "Herstarten (afwijzen = overslaan)"}},
            {"id": "wait", "kind": "wait", "minutes": 5, "title": {"en": "Wait for the server", "nl": "Wachten op de server"}},
            {"id": "check_after", "kind": "tool", "tool": "diagnose_server", "args": {"server": "{{params.server}}"},
             "title": {"en": "Health check after", "nl": "Gezondheidscheck achteraf"}},
        ],
    },
    {
        "key": "new_employee",
        "category": "onboarding",
        "name": {"en": "New employee", "nl": "Nieuwe medewerker"},
        "description": {
            "en": "Create the Active Directory account (disabled, approval), set the password and licence by hand, "
                  "and note the new employee on the customer.",
            "nl": "Active Directory-account aanmaken (uitgeschakeld, goedkeuring), wachtwoord en licentie handmatig "
                  "regelen en de nieuwe medewerker bij de klant noteren.",
        },
        "params": [
            {"name": "first_name", "label": {"en": "First name", "nl": "Voornaam"}, "required": True},
            {"name": "last_name", "label": {"en": "Last name", "nl": "Achternaam"}, "required": True},
            {"name": "username", "label": {"en": "User name (sAMAccountName)", "nl": "Gebruikersnaam (sAMAccountName)"},
             "required": True},
            {"name": "department", "label": {"en": "Department", "nl": "Afdeling"}, "required": False, "default": ""},
            {"name": "dc", "label": {"en": "Domain controller", "nl": "Domeincontroller"}, "required": True},
        ],
        "steps": [
            {"id": "intake", "kind": "manual", "assignee": "human",
             "title": {"en": "Check the details", "nl": "Gegevens controleren"},
             "instructions": {
                 "en": "Check with the customer: start date, function, department {{params.department}}, which groups, "
                       "mailbox and licence {{params.first_name}} {{params.last_name}} needs.",
                 "nl": "Check bij de klant: startdatum, functie, afdeling {{params.department}}, welke groepen, "
                       "mailbox en licentie {{params.first_name}} {{params.last_name}} nodig heeft."}},
            {"id": "ad_user", "kind": "tool", "tool": "run_command", "approval": "always",
             "title": {"en": "Create the AD account (disabled)", "nl": "AD-account aanmaken (uitgeschakeld)"},
             "args": {"node": "{{params.dc}}", "timeout": 120,
                      "reason": "Runbook new employee: {{params.first_name}} {{params.last_name}}",
                      "command": "Import-Module ActiveDirectory; "
                                 "New-ADUser -Name '{{params.first_name}} {{params.last_name}}' "
                                 "-GivenName '{{params.first_name}}' -Surname '{{params.last_name}}' "
                                 "-SamAccountName '{{params.username}}' "
                                 "-UserPrincipalName ('{{params.username}}@' + (Get-ADDomain).DNSRoot) "
                                 "-Department '{{params.department}}' -Enabled $false -ChangePasswordAtLogon $true; "
                                 "Get-ADUser '{{params.username}}' | Select-Object Name,SamAccountName,"
                                 "UserPrincipalName,Enabled | Format-List"}},
            {"id": "password", "kind": "manual", "assignee": "human",
             "title": {"en": "Password, groups and licence", "nl": "Wachtwoord, groepen en licentie"},
             "instructions": {
                 "en": "Set a first password for {{params.username}} and enable the account, add the groups, and "
                       "assign the Microsoft 365 licence and mailbox. Give the password to the employee in person.",
                 "nl": "Stel een eerste wachtwoord in voor {{params.username}} en schakel het account in, voeg de groepen "
                       "toe en wijs de Microsoft 365-licentie en mailbox toe. Geef het wachtwoord persoonlijk door."}},
            {"id": "note", "kind": "tool", "tool": "update_customer",
             "title": {"en": "Note it on the customer", "nl": "Bij de klant noteren"},
             "args": {"customer": "{{customer.id}}",
                      "append_note": "New employee {{params.first_name}} {{params.last_name}} "
                                     "({{params.username}}) set up by runbook {{run.id}}."}},
        ],
    },
    {
        "key": "github_project",
        "category": "project",
        "name": {"en": "New project via GitHub", "nl": "Nieuw project via GitHub"},
        "description": {
            "en": "Create a private repository through the GitHub MCP server (MCP hub), push the first code, then "
                  "set up the deployment into a container and deploy it.",
            "nl": "Privérepository aanmaken via de GitHub MCP-server (MCP hub), de eerste code pushen, daarna de "
                  "deployment naar een container instellen en uitrollen.",
        },
        "params": [
            {"name": "owner", "label": {"en": "GitHub owner (user or organisation)", "nl": "GitHub-eigenaar (gebruiker of organisatie)"},
             "required": True},
            {"name": "repo", "label": {"en": "Repository name", "nl": "Naam repository"}, "required": True},
            {"name": "description", "label": {"en": "Description", "nl": "Omschrijving"}, "required": False, "default": ""},
            {"name": "node", "label": {"en": "Proxmox node", "nl": "Proxmox-node"}, "required": True},
            {"name": "guest", "label": {"en": "Container (name or VMID)", "nl": "Container (naam of VMID)"}, "required": True},
            {"name": "deploy_cmd", "label": {"en": "Deploy command", "nl": "Deploy-commando"}, "required": True,
             "default": "./deploy.sh"},
        ],
        "steps": [
            {"id": "repo", "kind": "tool", "tool": "github__create_repository", "approval": "always",
             "title": {"en": "Create the repository", "nl": "Repository aanmaken"},
             "args": {"name": "{{params.repo}}", "description": "{{params.description}}", "private": True,
                      "autoInit": True}},
            {"id": "code", "kind": "manual", "assignee": "human",
             "title": {"en": "Push the first code", "nl": "Eerste code pushen"},
             "instructions": {
                 "en": "Push the first code to {{params.owner}}/{{params.repo}} and create the branches main "
                       "(production) and test. Make sure the deploy command works.",
                 "nl": "Push de eerste code naar {{params.owner}}/{{params.repo}} en maak de branches main "
                       "(productie) en test aan. Controleer dat het deploy-commando werkt."}},
            {"id": "deployment", "kind": "tool", "tool": "save_deployment",
             "title": {"en": "Set up the deployment", "nl": "Deployment instellen"},
             "args": {"name": "{{params.repo}}", "repo": "{{params.owner}}/{{params.repo}}", "node": "{{params.node}}",
                      "guest": "{{params.guest}}", "deploy_cmd": "{{params.deploy_cmd}}"}},
            {"id": "deploy", "kind": "tool", "tool": "run_deployment", "approval": "always",
             "title": {"en": "Deploy", "nl": "Uitrollen"},
             "args": {"deployment": "{{steps.deployment.result.id}}", "run_setup": True}},
        ],
    },
    {
        "key": "restart_service",
        "category": "remediation",
        "name": {"en": "Restart a failed service", "nl": "Vastgelopen service herstarten"},
        "description": {
            "en": "Read the status of a service, restart it, wait a minute and check that it runs. A repair action "
                  "that automatic recovery can reuse later.",
            "nl": "Status van een service lezen, herstarten, een minuut wachten en controleren dat hij draait. Een "
                  "herstelactie die automatisch herstel later kan hergebruiken.",
        },
        "params": [
            {"name": "server", "label": {"en": "Server", "nl": "Server"}, "required": True},
            {"name": "service", "label": {"en": "Service (unit)", "nl": "Service (unit)"}, "required": True},
        ],
        "steps": [
            {"id": "before", "kind": "tool", "tool": "get_service_status",
             "args": {"node": "{{params.server}}", "unit": "{{params.service}}"},
             "title": {"en": "Status before", "nl": "Status vooraf"}},
            {"id": "restart", "kind": "tool", "tool": "manage_service",
             "args": {"node": "{{params.server}}", "unit": "{{params.service}}", "action": "restart",
                      "reason": "Runbook {{run.id}}: restart {{params.service}}"},
             "title": {"en": "Restart the service", "nl": "Service herstarten"}},
            {"id": "wait", "kind": "wait", "minutes": 1, "title": {"en": "Wait a minute", "nl": "Een minuut wachten"}},
            {"id": "after", "kind": "tool", "tool": "get_service_status",
             "args": {"node": "{{params.server}}", "unit": "{{params.service}}"},
             "title": {"en": "Status after", "nl": "Status achteraf"}},
        ],
    },
    {
        "key": "free_disk_space",
        "category": "remediation",
        "name": {"en": "Free disk space", "nl": "Schijfruimte vrijmaken"},
        "description": {
            "en": "Clean up package caches, journal entries older than 14 days and old temp files (Windows: temp files, "
                  "the update download cache and the component store), then check the machine again.",
            "nl": "Pakketcache, journal ouder dan 14 dagen en oude tijdelijke bestanden opruimen (Windows: tijdelijke "
                  "bestanden, de downloadcache van updates en de componentenopslag) en de machine daarna controleren.",
        },
        "params": [
            {"name": "server", "label": {"en": "Server", "nl": "Server"}, "required": True},
        ],
        "steps": [
            {"id": "cleanup", "kind": "tool", "tool": "cleanup_disk",
             "args": {"node": "{{params.server}}", "reason": "Runbook {{run.id}}: free disk space"},
             "title": {"en": "Clean up", "nl": "Opruimen"}},
            {"id": "after", "kind": "tool", "tool": "diagnose_server", "args": {"server": "{{params.server}}"},
             "on_failure": "continue", "title": {"en": "Health check after", "nl": "Gezondheidscheck achteraf"}},
        ],
    },
    {
        "key": "recover_offline_agent",
        "category": "remediation",
        "name": {"en": "Bring an offline agent back", "nl": "Offline agent terugbrengen"},
        "description": {
            "en": "For a server that runs as a VM or container on a Proxmox VE node: start the guest when it is stopped, "
                  "or restart the agent inside it, then wait for it to reconnect. Never touches a Proxmox VE node.",
            "nl": "Voor een server die als VM of container op een Proxmox VE-node draait: start de guest als hij uit "
                  "staat, of herstart de agent erin, en wacht tot hij weer verbindt. Raakt nooit een Proxmox VE-node.",
        },
        "params": [
            {"name": "server", "label": {"en": "Server", "nl": "Server"}, "required": True},
        ],
        "steps": [
            {"id": "recover", "kind": "tool", "tool": "recover_agent",
             "args": {"node": "{{params.server}}", "reason": "Runbook {{run.id}}: the agent is offline"},
             "title": {"en": "Start the guest or restart the agent", "nl": "Guest starten of agent herstarten"}},
            {"id": "wait", "kind": "wait", "minutes": 3,
             "title": {"en": "Wait for the agent", "nl": "Wachten op de agent"}},
        ],
    },
    {
        "key": "update_agent",
        "category": "remediation",
        "name": {"en": "Update the agent", "nl": "Agent bijwerken"},
        "description": {
            "en": "Update the Stackpilot agent on a machine to the version of this Stackpilot.",
            "nl": "De Stackpilot-agent op een machine bijwerken naar de versie van deze Stackpilot.",
        },
        "params": [
            {"name": "server", "label": {"en": "Machine", "nl": "Machine"}, "required": True},
        ],
        "steps": [
            {"id": "update", "kind": "tool", "tool": "update_node_agent", "args": {"node": "{{params.server}}"},
             "title": {"en": "Update the agent", "nl": "Agent bijwerken"}},
        ],
    },
]


def _tr(v: Any, lang: str) -> Any:
    if isinstance(v, dict) and set(v) <= {"en", "nl"} and v:
        return v.get(lang) or v.get("en")
    return v


def localized(t: T, lang: str) -> T:
    """The template in one language, in the shape of a runbook (name, description, params, steps)."""
    lang = "nl" if lang == "nl" else "en"
    out = copy.deepcopy(t)
    out["name"] = _tr(out["name"], lang)
    out["description"] = _tr(out["description"], lang)
    out["params"] = [{**p, "label": _tr(p.get("label"), lang)} for p in out["params"]]
    out["steps"] = [{**s, "title": _tr(s.get("title"), lang), "instructions": _tr(s.get("instructions"), lang) or ""}
                    for s in out["steps"]]
    return out


def get(key: str, lang: str) -> T | None:
    t = next((t for t in TEMPLATES if t["key"] == key), None)
    return localized(t, lang) if t else None


def all_localized(lang: str) -> list[T]:
    return [localized(t, lang) for t in TEMPLATES]
