Versuch: 1/5
Punkte: 0/7

# Loop-Protokoll: Mehrfachfilter, Sammelbearbeitung, Kopplung ES + SOAR (MyDemoTicket)

## Kopf
- Start: 2026-11-25 ca. 08:20 CET
- Branch: feature/bulk-filter-pairing (nie auf main)
- Worktree: /Users/abuis/git/MyDemoTicket-pairing auf feature/bulk-filter-pairing-wizard
- Basis: main b501600
- Regeln: .quorda/loop/user-profil-i18n-rollen.md (Orchestrator, Inspektor sonnet/medium, Budget 5 Versuche / 3 h)

## Spec
Wortlaut von Andreas (2026-11-25):

> in der ticket ansicht ageal awelcher filte rmochte meher fach auswahl haben durch ein anhack box. dinge wie stat setzen
> fuer all priority assigement assing to setzen kone nalso bact verbaeitung - denke drann das es immer zu es gespiegelt
> werden muss wnen diese verbudne ist. da fellt mir ein in den admin hatte ich geren ein pairung setup mit ES + SOAR wo cih
> die daten hinertelgen kann die man brasucht. so wening wie moeglich auf der console

Ausgelegte Spec (verbindlich für die Agenten):
1. Ticketliste: jeder Filter (Status, Priorität, Zuweisungsgruppe, Bearbeiter, …) als Mehrfachauswahl mit Checkboxen;
   API akzeptiert mehrere Werte je Filter.
2. Sammelbearbeitung: Tickets per Checkbox (inkl. „alle auf der Seite“) wählen, dann Status / Priorität /
   Zuweisungsgruppe / Bearbeiter für alle setzen. Je Ticket eigenes Event + Journal + Webhook, damit SOAR/ES-Spiegelung
   für jedes Ticket greift; Ergebnis je Ticket (ok/Fehler) anzeigen; Berechtigungen je Ticket geprüft. DE/EN.
3. Admin „Kopplung ES + SOAR“ (Assistent): Eingabe SOAR-URL, SOAR-Automation-Token, Namen der Splunk-/ES-Assets in SOAR,
   Label. Prüft Verbindung, legt dann automatisch an: Integrations-User + API-Token im Ticket-System, Webhook Typ soar,
   SOAR-Label, MyDemoTicket-App (Bundle) + Asset in SOAR, beide Playbooks (importiert, aktiv). Zeigt Status je Schritt,
   wiederholbar ohne Dubletten. Tokens nie ausgegeben.
4. Live mit Cloud SOAR (Mandant audi) verifiziert: Sammeländerung an 2 Tickets erscheint in SOAR und ES.

## Checkliste
| # | Punkt | Nachweis | Stand |
|---|---|---|---|
| 1 | Mehrfachfilter per Checkbox (Status, Priorität, Gruppe, Bearbeiter), API mehrwertig | Code + Tests | offen |
| 2 | Sammelbearbeitung Status/Priorität/Gruppe/Bearbeiter, je Ticket eigenes Event (Spiegelung), Rechte je Ticket | Tests | offen |
| 3 | Kopplungs-Assistent ES+SOAR: prüfen + einrichten (User/Token, Webhook, Label, App, Asset, Playbooks aktiv), idempotent, Token nie ausgegeben | Tests gegen Fake-SOAR | offen |
| 4 | UI DE/EN vollständig für alle neuen Texte | test_i18n grün + Code | offen |
| 5 | Alle Tests grün (ticket-system, soar-app, mcp-server, e2e, deploy) | Erfolgszeilen im Volloutput | offen |
| 6 | Live: Deploy 1.2.0; Assistent gegen Cloud-SOAR (Mandant audi) ohne Dubletten; Sammeländerung an 2 Tickets erscheint in SOAR und ES | Live-Belege im Protokoll | offen |
| 7 | Kein Secret im Diff; Protokoll vollständig; jedes externe Werkzeug vermerkt | Protokoll + Diff | offen |

## Antworten auf Vorab-Fragen
- Keine Vorab-Fragen.
- Andreas startete den Loop mit „Arbeite den Auftrag in .quorda/loop/bulk-filter-pairing.md ab“.

## Aufwand
- Umgebungsvariable CLAUDE_CODE_EFFORT_LEVEL ist nicht gesetzt – der Parameter effort wirkt.
- Protokoll-Agent: model haiku, effort medium – nicht low, weil Protokoll-Agenten auf low/medium im Vorloop den Punkt
  „Protokoll“ verfehlten.
- Agent A (Filter + Sammelbearbeitung): effort medium – Bauen nach Spec.
- Agent B (Kopplungs-Assistent): effort medium – Bauen nach Spec; SOAR-REST-Ablauf ist in docs/installation.md beschrieben.
- Inspektor: model sonnet, effort medium (ab Versuch 1 nach Bedarf).
- Orchestrator (Merge, Deploy, Live-Prüfung, Aufräumen): eigene Sitzung, Aufwand nicht verstellt (bleibt bei Andreas).

## Annahmen
1. Sammelbearbeitung nutzt denselben Code-Pfad wie PATCH je Ticket, damit Webhook → SOAR → ES je Ticket greift.
2. Der Assistent benötigt ein SOAR-Automation-Token, das Andreas bzw. der Orchestrator aus dem Keychain eingibt; es wird
   nur an das zugehörige SOAR gesendet.
3. App und Playbooks werden im Deploy-Bundle mitgeliefert.

## Externe Werkzeuge
Noch keine in diesem Loop.

## Versuch 1
- Agent A (Filter + Sammelbearbeitung) und Agent B (Kopplungs-Assistent) gestartet.
- Ergebnis offen.

## Braucht Andreas
Noch nichts.
