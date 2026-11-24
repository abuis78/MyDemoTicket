Versuch: 2/5
Punkte: 4/6

# Loop-Protokoll: DEMEO Tecieket Ting system (MyDemoTicket)

Start: 2026-11-24 15:29 CET · Branch: feature/mydemoticket-loop · Basis: main 1249cf8

## Spec
Ziel: Ein etciketeing system welches ich feri demos nuztenkann - es muss per API (SOAR ansprechbar sein. Bedeute eine sTicket ing system welches Service now aenlich ist,. (nur nicht in dem umfang) welcches ich per SOAR asnprechen kann (also SOAR braicht dann eine eigen app) wo ich ein Ticket beantworten kann, status wehcslen kann was halt so dazu gehoert. Sindn ist es das SOAr von SPLUNk zwischen SPLUNK ES + SOAr und dem neuesn Ticket Sytemen Name: “MyDemoTciekt”. immer scnron ist. ich mus die oation fuer custom fidles haben es muss auch bei schanges diese ins soar sendne und soar muss es dann in ES spiegeln. - VErshcieden user level / rollen / teams

- Ertselle eine Architektur, mokkups wen dies freigegene sind erstelel die apps kompletet autonom. nuzte dazu die MCP umgebungen fuer Splunk und soar und AWS umg ggf, eine kleine Inztanz hochzuziehen die dafuer benotigt wird. dichte eine subdoomain ein https://ticket.soc-games.com.

Nuzte sub agent teams du bist orchestrator

## Checkliste
1. Ticket System gebaut → erfolgreich
2. SOAR App gebaut → erfolgreicht
3. Selber nehrfach gestest end-to-end in beide richtugen → getestet
4. Kein Secret im Diff → im Protokoll und im Diff nachlesen
5. Protokoll geschrieben und vollständig → im Protokoll und im Diff nachlesen
6. Jedes externe Werkzeug im Protokoll vermerkt → im Protokoll und im Diff nachlesen

## Antworten auf Vorab-Fragen
- Vor dem Loop keine Vorab-Antworten.
- ca. 15:45, während Versuch 1: Andreas: „setz python_version auf 3.13“ → umgesetzt in soar-app/mydemoticket/mydemoticket.json (Commit a69721d).

## Aufwand
- Umgebungsvariable CLAUDE_CODE_EFFORT_LEVEL ist nicht gesetzt (geprüft 15:29) – der Parameter effort wirkt.
- Versuch 1 · Protokoll-Agent: model haiku, effort low – mechanisch.
- Versuch 1 · Agent A (Ticket-System bauen): effort medium – Bauen nach klarer Spec (Architektur vom Orchestrator vorgegeben).
- Versuch 1 · Agent B (SOAR-App bauen): effort medium – Bauen nach klarer Spec.
- Versuch 1 · Agent B Nachauftrag (python_version 3.13): gleicher Agent, effort medium (fortgesetzt, nicht neu gestartet).
- Versuch 1 · Agent C (End-to-end-Tests beide Richtungen bauen und 3× ausführen): effort medium – Bauen nach klarer Spec.
- Versuch 1 · Protokoll-Nachtrag: model haiku, effort low – mechanisch.
- Versuch 1 · Agent A umfasste auch Architektur-Doku, Mockups (docs/mockups/) und Deploy-Dateien (deploy/): effort medium – alles nach vorgegebener Spec.
- Versuch 1 · Agent C fand und behob einen Fehler (c4d1e4e), effort blieb medium – kein gescheiterter Nachweis.
- Versuch 1 · Inspektor: model sonnet, effort medium – wie im Auftrag vorgegeben.
- Versuch 2 · Protokoll-Agent: model haiku, effort medium – HOCHGESTUFT von low auf medium, weil der Inspektor in Versuch 1 die Punkte 5 und 6 (Protokoll) nicht bestanden ließ.
- Versuch 2 · Inspektor: model sonnet, effort medium – wie im Auftrag vorgegeben.

## Annahmen
1. „Mockups erst nach Freigabe“: Der Loop-Auftrag gilt als Freigabe („Dieser Auftrag ist die Freigabe“). Mockups werden als statische HTML-Dateien unter docs/mockups/ erstellt, der Bau läuft ohne Warten weiter.
2. „Keine neue Abhängigkeit“: Ticket-System und SOAR-App nutzen nur die Python-Standardbibliothek (http.server, sqlite3, urllib, hmac). Die Web-Oberfläche ist reines HTML/CSS/JS ohne Framework.
3. Synchronisation Ticket → SOAR: Push per Webhook bei jeder Änderung (Typ „soar“ legt über die SOAR-REST-API Container/Artefakte an) plus Fallback „on poll“ in der SOAR-App. Synchronisation SOAR → Ticket: Aktionen der SOAR-App. „Immer synchron“ = nahezu Echtzeit per Push, Poll als Absicherung.
4. Spiegelung SOAR → Splunk ES: als SOAR-Playbook-Code im Repo; ohne Freigabe für Live-Systeme nicht gegen echtes SOAR/ES getestet.
5. End-to-end-Tests laufen lokal: echter Ticket-Server + echter App-Connector-Code gegen einen lokalen SOAR-Nachbau (Mock-Server + phantom-Stub). Tests gegen das echte SOAR/ES sind geparkt (siehe Braucht Andreas).
6. Rollen: admin, agent, requester, integration; Teams als Zuweisungsgruppen.
7. Agent A: Ein Request erzeugt genau ein Webhook-Event; soar-Webhooks liefern Events mit source „soar“ nicht aus (Loop-Schutz, Status „skipped“), generic-Webhooks schon.
8. Agent A: Der SOAR-Webhook-Secret (ph-auth-token) liegt im Klartext in SQLite (muss an SOAR gesendet werden), wird von der API nie zurückgegeben; API-Tokens und Passwörter nur gehasht.
9. Agent A: Admin-Nutzer wird auch mit --no-seed angelegt; Zugangsdaten nur in <datadir>/initial-credentials.txt (0600), nie auf stdout.
10. Agent B: on poll überspringt ein Ticket nur, wenn der neueste Journal-Eintrag source == source_tag hat; Snapshot-Artefakte mit Label mydemoticket_snapshot und run_automation false.
11. Agent B: ES-Mirror-Playbook nutzt die Splunk-Aktion „update event“ mit integer_status; Parameternamen nicht gegen die echte Splunk-App geprüft, Import in SOAR nicht geprüft.
12. Web-UI nur per HTTP-Tests geprüft, nicht in einem echten Browser durchgeklickt.
13. Agent C: Legt SOAR ein Ticket mit der Container-ID eines ES-Containers an, überschreibt die erste Push-Zustellung soar_container_id mit der ID des Ticket-Containers (sdi = Ticketnummer). Dokumentiertes Verhalten, im E2E so geprüft.
14. Das gebaute Paket soar-app/dist/mydemoticket.tgz ist nicht versioniert; vor der Installation mit `python3 soar-app/build.py` neu bauen.
15. End-to-end „beide Richtungen“ ist lokal nachgewiesen (echter Server + echter Connector gegen SOAR-Mock); gegen echtes SOAR/ES geparkt.

## Externe Werkzeuge
| Zeit | Wer | Werkzeug | Ziel | Zweck |
|---|---|---|---|---|
| 15:29 | Orchestrator | git fetch --all --prune | github.com/abuis78/MyDemoTicket | Stand holen (ohne Rückfrage erlaubt) |
| 15:29 | Orchestrator | gh pr list --state open | GitHub API, Repo abuis78/MyDemoTicket | Stand holen (offene PRs, Ergebnis leer) – lesend |
| 15:29 | Orchestrator | gh issue list --state open | GitHub API, Repo abuis78/MyDemoTicket | Stand holen (offene Issues, Ergebnis leer) – lesend |
| 16:00 | Orchestrator | git fetch | github.com/abuis78/MyDemoTicket | Stand holen vor Versuch 2 |

Geplant nach Bestehen (wird nach Ausführung hier bestätigt): git push des Branches feature/mydemoticket-loop und gh pr create in diesem Repository (beide ohne Rückfrage erlaubt).

Nicht genutzt: keine MCP-Werkzeuge (Splunk, SOAR, AWS), kein WebFetch/WebSearch, kein curl/ssh nach außen.
Agenten A, B, C, Protokoll-Agenten und Inspektor: keine externen Werkzeuge; Netz nur 127.0.0.1 (lokale Test-Server und Mocks). Lokale Werkzeuge ohne Netz: python3, git (lokal), node --check (Syntaxprüfung app.js, Agent A), Skill soar-app-builder (lokale Anleitung, Agent B).
Netzwerkziele im Code sind nur konfigurierbar (kein fest verdrahtetes Ziel): ticket-system/mydemoticket/webhooks.py (Webhook-Zustellung), soar-app/mydemoticket/mydemoticket_connector.py (Ticket-API).

## Versuche
(Urteile des Inspektors je Versuch folgen hier.)

### Versuch 1 – Bau (Berichte der Agenten, noch kein Inspektor-Urteil)
- Agent A Ticket-System: Commits e3356f8, 7369905, d7d69f3, f32b0a9, bd5e2fd, 6b84242, 8ffb986, 9e3e5a7, d2b9d33, 80ea0f8, db32781, a1f657e; laut Agent: „Ran 61 tests … OK“.
- Agent B SOAR-App + Playbooks: Commits ee8467b, 241c996, d79ac15, ba0e868, 1c099db, cc5cec7, a69721d; laut Agent: „Ran 49 tests … OK“.
- Agent C End-to-end: Commits c4d1e4e (fix Severity-Mapping im Connector), edbd69d (tests/e2e/, scripts/e2e.sh), 305f24a (README-Abschnitt); laut Agent: 3× „E2E run i/3: OK“, je „Ran 8 tests … OK“.
- Protokoll: fd92821, e5c60c3.

### Versuch 1 – Urteil des Inspektors (sonnet, medium)
| Punkt | Urteil | Beleg (vom Inspektor selbst ausgeführt) |
|---|---|---|
| 1 Ticket System gebaut | BESTANDEN | `python3 -m unittest discover -s tests -v` in ticket-system: „Ran 61 tests in 11.294s“, „OK“; Spec-Abdeckung laut docs/api.md |
| 2 SOAR App gebaut | BESTANDEN | `python3 -m unittest discover -s tests -v` in soar-app: „Ran 49 tests“, „OK“; 10 Actions im Manifest; Playbooks vorhanden (ES-Spiegelung nur als Code) |
| 3 E2E mehrfach, beide Richtungen | BESTANDEN | `bash scripts/e2e.sh 3`: exit 0, „E2E run 1/3: OK“ … „E2E run 3/3: OK“, je „Ran 8 tests“, „OK“ (lokaler SOAR-Mock) |
| 4 Kein Secret im Diff | BESTANDEN | grep auf `git diff main...HEAD`: nur Platzhalter/Feldnamen, keine Schlüssel, keine credentials-/.env-/.db-Dateien in Git |
| 5 Protokoll vollständig | NICHT BESTANDEN | Versuchsbericht veraltet (Agent C „läuft“, E2E-Commits fehlten), Aufwand für Inspektor/Mockups/Deploy fehlte, Punkte-Zeile nicht aktualisiert |
| 6 Externe Werkzeuge vermerkt | NICHT BESTANDEN | Tabelle nannte nur git fetch 15:29; weitere Orchestrator-Aufrufe nicht belegt |
Außerhalb des Umfangs: kein Befund. Tests abgeschwächt/gelöscht: kein Befund (a69721d passt Test an Andreas' Vorgabe python_version 3.13 an).
Ergebnis Versuch 1: 4/6.

### Versuch 2 – Protokoll-Nachträge (Punkte 5, 6)
- Externe Werkzeuge vollständig nachgetragen (gh pr list, gh issue list 15:29; git fetch 16:00), Aufwand und Versuchsbericht ergänzt. Urteil des Inspektors folgt.

## Braucht Andreas
1. AWS-Instanz anlegen (kostenpflichtig, extern) – nicht ausgeführt; Deploy-Skript liegt im Repo.
2. DNS-Eintrag ticket.soc-games.com (extern, one.com) – nicht ausgeführt.
3. SOAR-App auf dem echten SOAR installieren und Live-Test SOAR ↔ Ticket ↔ ES (Schreibzugriff auf Live-Systeme) – nicht ausgeführt.
4. Web-Oberfläche einmal im Browser durchklicken (nur per HTTP-Tests geprüft).
5. ES-Mirror-Playbook: Parameternamen der Splunk-Aktion „update event“ (integer_status u. a.) gegen die installierte Splunk-App prüfen und Playbooks in SOAR importieren.
