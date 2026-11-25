Versuch: 1/5
Punkte: 0/9

# Loop-Protokoll: Benutzerverwaltung, Profil, DE/EN, Rollenverwaltung, Splunk-Verbindung (MyDemoTicket)

## Kopf
- Start: 2026-11-25 ca. 07:00 CET
- Branch: feature/user-profile-i18n-roles (nie auf main)
- UI-Worktree: /Users/abuis/git/MyDemoTicket-ui auf Branch feature/user-profile-i18n-roles-ui
- Basis: main 29872e7

## Spec
Wortlaut von Andreas (2026-11-25):

> drosel bitte die user verwaltung noch etwas auf wen ich auf set password gehe soll das neue password zweimal angefragt
> werden und der display name soll ander bar sein. ausserdem wnen cih oben rechts auf den user name klicke soll dieser sein
> sachen anapssne konnen ausser dem zeitzone, und zwischen datkmode und hhellen wechslen konne und die sprache wahlen konne.
> ich willd as das ticket system wechslen kann zwischen deustch und english das bedeutet das geammte ticket sytem solin der ui
> zwei sprahen haben. Eine Rolen verwaltung soll es geben, die bestehendne rollen aber auch ein paar rollen die nur Analysten
> entsprechen also kein admin kein etennan verwaltung usw. - arbete alles im loop ab erledigt ist wenn es wie beschrieben klappt

Nachträge von Andreas während des Starts:
- „das alnelegen einse user klappt nicht“
- „unter audit / splunk mochte ich die option haben … die verbindung zum splunk anzu legen“

Ausgelegte Spec (verbindlich für die Agenten):
1. Set password (Admin, Benutzerliste): Dialog/Formular statt window.prompt, neues Passwort zweimal; ungleich oder < 8 Zeichen → kein Request, verständliche Meldung.
2. Display name (und E-Mail) in der Benutzerliste durch Admins änderbar (API kann es schon: PATCH /users/<u>).
3. Profil: Klick auf den Namen oben rechts öffnet #/profile. Eigene Angaben ändern: Display name, E-Mail, eigenes Passwort (aktuelles + neues zweimal), Zeitzone (IANA, leer = Browser), Darstellung hell/dunkel/System, Sprache Deutsch/English. Gespeichert am User (Server), wirkt sofort. Zeitzone wirkt auf alle angezeigten Zeiten.
4. Bug: Eigenes Passwort ändern (Profil oder Admin-Set-Password am eigenen Konto) darf die laufende Sitzung nicht beenden; andere Sitzungen dieses Users werden beendet. Benutzer anlegen muss danach funktionieren.
5. Gesamte UI zweisprachig DE/EN (alle sichtbaren Texte, Labels, Toasts, Fehlertexte der UI, Status-/Rollennamen, Login). Login-Seite: zuletzt gewählte Sprache (localStorage) sonst Browser-Sprache.
6. Rollenverwaltung (#/admin/roles): zeigt alle Rollen mit Berechtigungen. Bestehende Rollen bleiben unverändert im Verhalten (platform_admin, admin, agent, requester, integration). Neue eingebaute Analysten-Rollen: analyst_l1, analyst_l2, soc_lead, read_only. Admins können eigene Rollen je Mandant anlegen/ändern/löschen, aber nur aus Analysten-Berechtigungen (keine Benutzer-, Rollen-, Team-, Feld-, Webhook-, Mandanten-, Splunk-Verwaltung). Serverseitig erzwungen.
7. Audit / Splunk: platform_admin kann die Splunk-HEC-Verbindung in der UI anlegen/ändern (URL, Index, Token nur schreibbar, TLS-Prüfung, aktiv) und testen. Token nie in API-Antworten/Logs. Gespeicherte Einstellung hat Vorrang vor Umgebungsvariablen.

## Checkliste
| # | Punkt | Nachweis | Stand |
|---|---|---|---|
| 1 | Set password fragt zweimal, Display name änderbar | Code + Test | offen |
| 2 | Profil mit Display name, E-Mail, Passwort, Zeitzone, Theme, Sprache | API-Tests + UI-Code | offen |
| 3 | Eigene Passwortänderung beendet die eigene Sitzung nicht, User anlegen klappt danach | Test | offen |
| 4 | UI komplett DE/EN | Test: jeder Schlüssel in beiden Sprachen, keine fest verdrahteten englischen UI-Texte in app.js | offen |
| 5 | Rollenverwaltung inkl. Analysten-Rollen, nur Analysten-Rechte vergebbar, serverseitig erzwungen | Tests | offen |
| 6 | Splunk-Verbindung in Audit / Splunk anlegbar und testbar, Token nie ausgegeben | Tests | offen |
| 7 | Alle Tests grün (ticket-system, soar-app, mcp-server, e2e, deploy) | Erfolgszeilen im Volloutput | offen |
| 8 | Live auf https://ticket.soc-games.com deployt und geprüft | Health-Version + Live-Prüfungen im Protokoll | offen |
| 9 | Kein Secret im Diff; Protokoll vollständig; jedes externe Werkzeug vermerkt | Protokoll + Diff | offen |

## Antworten auf Vorab-Fragen
- Keine Vorab-Fragen.
- Nachträge während des Starts (siehe Spec): „das alnelegen einse user klappt nicht“ → Punkt 3 bzw. Diagnose unten. „unter audit / splunk … die verbindung zum splunk anzu legen“ (Präzisierung: die Verbindung zu Splunk anlegen) → Punkt 7 der Spec, Checkliste 6.

## Aufwand
- Umgebungsvariable CLAUDE_CODE_EFFORT_LEVEL ist nicht gesetzt (geprüft 07:00) – der Parameter effort wirkt.
- Versuch 1 · Protokoll-Agent: model haiku, effort low – mechanisch.
- Versuch 1 · Agent A (Backend): effort medium – Bauen nach Spec/Vertrag.
- Versuch 1 · Agent B (UI): effort medium – Bauen nach Spec/Vertrag.
- Inspektor: später model sonnet, effort medium.

## Annahmen
1. Default-Sprache für bestehende und neue User ist „de“, weil Andreas Deutsch nutzt. Die Login-Seite nimmt localStorage bzw. die Browsersprache.
2. Zeitzone leer = Browser-Zeitzone.
3. Analysten-Rollen als eingebaute Rollen: analyst_l1, analyst_l2, soc_lead, read_only. Eigene Rollen je Mandant werden nur aus Analysten-Berechtigungen gebildet.
4. Splunk-Verbindung = HEC-Verbindung des Audit-Loggings, nur für platform_admin. Der Token liegt wie der SOAR-Webhook-Secret im Klartext in SQLite und wird nie zurückgegeben.
5. Schema-Migration v4 ist additiv mit Backup (*.pre-v4.bak). Sie gilt nicht als verbotene Datenmigration.
6. Der API-Vertrag zwischen Backend und UI wird vom Orchestrator festgelegt (Datei im Scratchpad, Inhalt sinngemäß in der Spec).

## Diagnose
Nachtrag „User anlegen klappt nicht“:
- Splunk-Abfrage index=mydemoticket zeigt 06:46:37 user.update admin→admin (password), danach 06:50:42 POST /api/v1/users → 401 „authentication required“.
- Ursache: PATCH /users mit password löscht alle Sitzungen des Users, auch die eigene.
- Fix: Punkt 4 der Spec, Checkliste 3.

## Externe Werkzeuge
| Zeit | Wer | Werkzeug | Ziel | Zweck |
|---|---|---|---|---|
| ~06:55 | Orchestrator | mcp__splunk-socgames__splunk_run_query (3 Abfragen) | splunk.soc-games.com, Index mydemoticket | Fehler beim User-Anlegen finden – lesend |
| ~06:55 | Orchestrator | Bash curl POST https://ticket.soc-games.com/api/v1/auth/login als admin | ticket.soc-games.com | Prüfen, ob das gespeicherte Admin-Passwort gilt (HTTP 200). Passwort aus Keychain, nicht ausgegeben |

## Versuch 1
- Agent A (Backend) und Agent B (UI) gestartet. Ergebnis offen.
- Urteil des Inspektors: noch nicht erstellt.

## Braucht Andreas
1. Das Admin-Passwort wurde um 06:46 in der UI geändert. Der Keychain-Eintrag mydemoticket/admin-password ist vermutlich veraltet. Empfehlung: Eintrag mit dem neuen Passwort aktualisieren.
2. Für den Deploy ggf. `aws sso login --profile janus-env2`.
