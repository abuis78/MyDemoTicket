Versuch: 1/5
Punkte: 0/6

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
Keine – im Verlauf standen keine Vorab-Antworten von Andreas.

## Aufwand
- Umgebungsvariable CLAUDE_CODE_EFFORT_LEVEL ist nicht gesetzt (geprüft 15:29) – der Parameter effort wirkt.
- Versuch 1 · Protokoll-Agent: model haiku, effort low – mechanisch.
- Versuch 1 · Agent A (Ticket-System bauen): effort medium – Bauen nach klarer Spec (Architektur vom Orchestrator vorgegeben).
- Versuch 1 · Agent B (SOAR-App bauen): effort medium – Bauen nach klarer Spec.

## Annahmen
1. „Mockups erst nach Freigabe“: Der Loop-Auftrag gilt als Freigabe („Dieser Auftrag ist die Freigabe“). Mockups werden als statische HTML-Dateien unter docs/mockups/ erstellt, der Bau läuft ohne Warten weiter.
2. „Keine neue Abhängigkeit“: Ticket-System und SOAR-App nutzen nur die Python-Standardbibliothek (http.server, sqlite3, urllib, hmac). Die Web-Oberfläche ist reines HTML/CSS/JS ohne Framework.
3. Synchronisation Ticket → SOAR: Push per Webhook bei jeder Änderung (Typ „soar“ legt über die SOAR-REST-API Container/Artefakte an) plus Fallback „on poll“ in der SOAR-App. Synchronisation SOAR → Ticket: Aktionen der SOAR-App. „Immer synchron“ = nahezu Echtzeit per Push, Poll als Absicherung.
4. Spiegelung SOAR → Splunk ES: als SOAR-Playbook-Code im Repo; ohne Freigabe für Live-Systeme nicht gegen echtes SOAR/ES getestet.
5. End-to-end-Tests laufen lokal: echter Ticket-Server + echter App-Connector-Code gegen einen lokalen SOAR-Nachbau (Mock-Server + phantom-Stub). Tests gegen das echte SOAR/ES sind geparkt (siehe Braucht Andreas).
6. Rollen: admin, agent, requester, integration; Teams als Zuweisungsgruppen.

## Externe Werkzeuge
| Zeit | Werkzeug | Ziel | Zweck |
|---|---|---|---|
| 15:29 | git fetch | github.com/abuis78/MyDemoTicket | Stand holen (ohne Rückfrage erlaubt) |

## Versuche
(Urteile des Inspektors je Versuch folgen hier.)

## Braucht Andreas
1. AWS-Instanz anlegen (kostenpflichtig, extern) – nicht ausgeführt; Deploy-Skript liegt im Repo.
2. DNS-Eintrag ticket.soc-games.com (extern, one.com) – nicht ausgeführt.
3. SOAR-App auf dem echten SOAR installieren und Live-Test SOAR ↔ Ticket ↔ ES (Schreibzugriff auf Live-Systeme) – nicht ausgeführt.
