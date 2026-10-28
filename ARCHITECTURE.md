# Architecture

One call session, fed text by whichever transport is in use, surrounded by
ordinary code that enforces every rule. The model decides what Sunday says
next and what the transcript meant; code decides when the call is an
emergency, when it ends, what the family is told and when, and what counts
as evidence.

```
cron ──► sunday due ──► dialer (Twilio) ──► parent's phone ◄── or the parent rings "Sunday"
                                                 │  speech ⇄ text (Twilio Conversation Relay)
                                                 ▼
          transport (Twilio WebSocket | terminal simulator | test harness)
                                                 │  start / hear / hangup
                                                 ▼
                                           CallSession
                      ┌──────────────────────────┼──────────────────────────┐
                      ▼                          ▼                          ▼
             brain/safety.py           brain/conversation.py          repository.py
         keyword triggers, tiers,     plan, memories, follow-ups,    every turn written
         emergency script             family messages, next line     as it happens
                                                 │
                                        call ends (any reason)
                                                 ▼
                                      notify/pipeline.finish_call
                   now-alerts sent first ──► analysis ──► store ──► notes to family
                                                                        │
                                                            notify/digest (weekly)
```

## Modules

| Path | Role |
|---|---|
| `geo.py` | Where Sunday operates: the United States. `check_callable` is run by every path that can ring a phone (dialer, "Have Sunday call now", the CLI, the scheduler, a parent calling in) and refuses anything else before a Call row exists; Canada and the Caribbean share +1 and are refused on the full number. Also timezone-from-area-code and US number formatting. Family members who only receive notes may be anywhere. |
| `models.py`, `repository.py` | Parent, FamilyMember, Medication, Memory, QueuedMessage, Call, Turn, Observation, Alert on local SQLite. One thin repository owns every query; nothing else touches the database. On open it adds any columns a newer model has that an older database lacks. A queued message may carry `remind_on`, a local date before which it is held back; `parent_by_phone` matches an inbound caller on digits. |
| `config.py` | Provider, keys, budgets, DB path, from `.env`. |
| `brain/safety.py` | Deterministic keyword triggers in English, Spanish, Urdu, Hindi and Punjabi run on every utterance before any model. Fall, chest pain, breathing, stroke signs, bleeding, "call an ambulance", scam in progress → `now`. Severe pain, fever, confusion, low mood, medication concern, scam attempt, no food → `today`. Denials ("no pain today") and food that is on its way ("haven't eaten yet, I'll make something") keep `today` triggers quiet; a `now` trigger is never softened. `SafetyState` dedupes and tracks the highest tier. The emergency script lines and the emergency number per country (derived from the parent's phone prefix) live here too. |
| `brain/conversation.py` | A loose plan (greeting, wellbeing, sleep, medication, meals, follow-ups, family messages, open talk, close) the model weaves through. Builds the system prompt from memories, yesterday's follow-ups and queued family messages. Fixed honest opening line per language. Replies trimmed to three sentences; the parent's name allowed in at most one of every three turns. A cheap bookkeeping call marks plan items covered and messages delivered; code only accepts a message as delivered if Sunday's own words contain it. |
| `brain/analysis.py` | One structured call turns the transcript into summary, typed observations, memories and alerts, each quoting the parent. Quotes not in the transcript are dropped. Alerts whose kind or text is a household matter become follow-up observations; household observations are always follow-ups; a reply to a family message never is; a tradesperson or delivery the parent said has not come is recorded even if the model forgot it. Code-detected signals are merged in and never downgraded. Also decides the no-answer tiers. |
| `brain/model.py`, `brain/offline.py` | `ChatModel` protocol with an OpenAI adapter and a scripted model for tests; `complete_json` validates against a Pydantic model with one repair attempt. The offline brain recognises the three prompt types and answers with rules, so every path runs with no key. |
| `calls/session.py` | The live-call state machine: `start`, `hear`, `hangup`. Phases talking → emergency / closing → ended. Turn and minute budgets. Persists every turn as it happens. Hands a `now` alert to an `on_alert` hook the instant it is persisted. An `inbound` session uses a shorter plan (how are you, anything waiting from the family, open talk) and a greeting that is glad they called; the safety triggers are identical. Never raises to the transport for model failures. |
| `calls/transport.py` | Twilio Conversation Relay: TwiML for the answered call, `/twilio/inbound` for a call the parent places (caller id → parent, or a kind refusal), a WebSocket that receives final speech-to-text and sends text to speak, and the hang-up message. When the socket closes it runs `on_end`, so the after-call pipeline never depends on Twilio's status callback being configured. The terminal simulator drives the same session. |
| `calls/dialer.py`, `scheduler.py` | `Telephony` protocol (place a call, start a recording, fetch a recording) with Twilio and fake implementations. `is_due` rings at the parent's local call time, retries once fifteen minutes after an unanswered first ring, and never a third time that day; a parent whose introductory call has not happened is never due. `digest_due` is Sunday evening local time, once a week. `record_outcome` maps Twilio status callbacks; `consecutive_no_answers` counts days, not rings, so two attempts on one morning are one day of silence: two days → `today`, three → `now`. |
| `notify/pipeline.py` | `notify_urgent`, called from inside a live call to send `now` alerts at once; and `finish_call`, the one function transports call when a call ends. Idempotent. Any unsent emergencies first, then analysis, storage, and a short note to each family member whose alert level wants it. |
| `notify/messenger.py` | WhatsApp or SMS via Twilio, or the console. One member's failure never blocks the others. |
| `notify/digest.py` | Trends over stored alerts and observations: the week's alerts first, then calls, medication, mood, sleep, "worth a look" flags, open follow-ups. Pure code, never a diagnosis. `send_digest` delivers it to members on `info` and stamps the parent so the cron tick sends it once. |
| `web/categories.py` | Pure functions that sort stored observations, alerts and queued messages into the six things a family member asks about, and write the one-line answer each tile shows ("Taken today. 7 of the last 7 days."). Medication is reconciled per local day, a later "taken" beating an earlier "missed"; appointments are dated reminders plus anything she said with a clinic word in it; health counts only the worrying values this week. Every item carries the parent's quote and the call it came from. |
| `web/` | The public landing page, pricing and trust pages (privacy, terms, security, consent, contact; drafts with the company name as a placeholder); self-serve onboarding as a three-step wizard with libphonenumber validation and timezone inferred from the parent's number; the home dashboard (greeting, one-sentence headline, an urgent banner when something is open, six tiles, the fortnight, the next-call panel); one app shell on every page (Home, Calls, Ask Sunday, Family, Settings; bottom bar on phones) with breadcrumbs below home; the live call page that polls `status.json` through ringing, connected, writing, ready; family sign-in by one-time link sent over the member's own channel (`auth.py`, signed with `itsdangerous`, single-use, rate limited, Secure HttpOnly session cookie); the family page and its actions, every one re-checking that the signed-in phone is on the parent's family record; the call page; a JSON export of everything; Twilio webhooks (`/twilio/voice`, `/twilio/inbound`, `/twilio/status`) behind `X-Twilio-Signature` verification (`twilio_auth.py`, fails closed) and the relay WebSocket, which only accepts a callSid a verified webhook registered. Server-rendered Jinja templates and one stylesheet; no JavaScript framework. |
| `cli.py`, `demo.py` | The operator CLI, including `serve` (loopback by default, refuses to start without `SUNDAY_SECRET_KEY`) and `demo`, which seeds a believable fortnight. |

## Three rules, enforced in code

1. **The parent never touches technology.** The phone rings; they answer. Twilio does speech both ways. There is no app, no login, nothing to charge or update.
2. **Safety does not depend on the model.** Keyword triggers run first on every utterance. A `now` signal persists the alert, sends it to the family through `on_alert` before Sunday says another word, switches the session to scripted lines in the parent's language that remind her she can dial 911, and keeps the line open; the model is never consulted during an emergency. The emergency script never hangs up, which is exactly why the alert cannot wait for the end of the call. If sending fails the call carries on and `finish_call` retries before its own model call. A `today` signal is recorded and the call continues normally. If the model is down the call ends with a fixed kind line and the transcript is kept.
3. **Nothing is invented.** Every observation, alert and memory must carry `evidence` that appears in the transcript; the rest are dropped before storage. The family summary is written from what survived. The opening line is fixed so Sunday always says it is an assistant sent by the family. The prompt forbids medical advice, dosages and asking for money, and the analysis step cannot produce them because it only has observation types.

## The call, turn by turn

1. Transport reports the call answered (or, for an inbound call, that the parent is on the line). `start` writes the opening line, marks `greeting` done. Queued family messages are loaded for the parent's local date, so a reminder dated for Tuesday is not read out on Monday.
2. Each final utterance goes to `hear`: it is persisted, then run through `keyword_signals`. Fresh signals are persisted as alerts at once; a `now` alert is also sent to the family right there, via the `on_alert` hook `Services` wires to the messenger.
3. If any `now` signal is active, the next scripted emergency line is returned and the call never closes on its own.
4. Otherwise budgets are checked (turns, minutes), then `update_progress` asks the model which plan items the parent has now covered and which family messages were delivered. Code applies three gates: an item is only marked if it is on the plan; a message is only delivered if at least half its content words appear in something Sunday actually said ("I'll let Hira know" does not deliver Hira's message); and an unanswered family `ask` keeps `family_messages` open even if the parent sounds tired.
5. If only `close` remains, the localised closing line is returned and the call ends; if anything was flagged this call the close says "how you're doing today" rather than "you're well". Otherwise `next_line` builds the system prompt (context, plan, what is done, pending asks, minutes so far) and gets one short reply, which is trimmed to three sentences and stripped of repeated use of the parent's name.
6. When the transport reports the line dropped or the session ends, `finish_call` runs the after-call pipeline.

## Context engineering

The model sees a small, fixed-shape prompt every turn: who the parent is, how to speak, the hard rules, memories, yesterday's follow-ups, pending family messages with ids and kind (`Tell them` / `ASK them and wait for their answer` / `Remind them`), the plan, what is covered, and the last 24 transcript turns. Family messages are placed early with an explicit instruction to deliver them right after asking how they are; when an `ask` is pending a one-line nudge is appended so it gets asked plainly.

The analysis prompt is given the whole transcript and the delivered-message ids and must return typed JSON. Alert tiers are defined in the prompt in terms of the person's wellbeing, with household matters explicitly excluded; code then re-checks every alert's kind and text and demotes household ones anyway.

## Who can see what

Three audiences, three mechanisms.

- **Anyone** sees the landing page, the sign-in form and the onboarding form. Nothing about any parent is on them. Onboarding creates a parent and one family member and sends that member a sign-in link; it is rate limited per address, refuses to create a second household for a known number, and the parent it creates has `intro_done=False`, so the scheduler will not ring the number until a signed-in family member has placed the introductory call. That is the guard against typing a stranger's number into a public form.
- **Family members** sign in with the phone number on their family record. We send a one-time link over the channel their notes already arrive on (WhatsApp, SMS, console in development), signed with the server secret, valid for fifteen minutes and once. The session is a signed, HttpOnly, SameSite cookie (Secure when the public URL is https). Authorization is membership: `Viewer.parents` is exactly the parents whose family record carries that phone, and every `/app` route resolves the parent through it, so another family's parent is a 404 even by id. A member can change only their own alert level. The form never reveals whether a number is known and is rate limited per number and per address.
- **Recordings** exist only when `Parent.consent_recording` is set by the family. The opening line then always includes the recording notice in the parent's language, recording starts from the verified answered-call webhook, Twilio's completion callback (also verified) stores the URL, and the family listens through `/app/{pid}/calls/{id}/recording.mp3`, which checks membership and fetches the audio with the account credentials; Twilio's URL is never shown. A callback for a parent whose consent has since been withdrawn is ignored.
- **Twilio** signs each webhook with the account auth token; `make_twilio_verifier` recomputes the signature over the public URL and form fields and rejects mismatches. With verification on and no token configured, every webhook is rejected. The Conversation Relay WebSocket cannot carry that signature, so it only accepts a `callSid` that a verified webhook registered moments before, and never builds a session from parameters the socket supplies.

The server binds to 127.0.0.1. A tunnel or reverse proxy is the public face; `serve` warns if asked to bind wider.

## Persistence

SQLite via SQLAlchemy at `SUNDAY_DB` (default `data/sunday.sqlite`, gitignored; conversations with a parent are sensitive and never leave the machine unless sent to the family). Every parent turn and Sunday turn is written as it happens, so a dropped call still leaves a transcript. Alerts raised during a call are persisted before anything is said back. `finish_call` is idempotent: a call that has already been notified is skipped, so a retry after a crash is safe. Family notes record what was sent to whom.

## Failure modes

| Situation | Behaviour |
|---|---|
| Parent says a `now` trigger phrase | Alert persisted and sent to everyone before the next line is spoken; scripted calm lines that mention 911; line stays open; model not consulted |
| A parent is added with a number outside the United States | Refused by the CLI and the sign-up form; if one is in the database anyway, `dial` raises before creating a Call, `due` skips it with a note, and an inbound call from it hears a short refusal |
| Sending the urgent alert fails (Twilio down) | Logged; the call continues; `finish_call` retries the send before anything else |
| Parent says "haven't eaten yet, I'll make something" or "no pain today" | No `today` alert; the `now` triggers are never softened this way |
| Parent says a `today` trigger phrase | Alert persisted; call continues; sent after the call with the summary to members on `today` or `info` |
| Model times out or errors mid-call | Fixed kind line in the parent's language, call ends with reason `model_error`, transcript kept, pipeline still runs |
| Model returns invalid JSON for progress | Ignored; the plan simply does not advance this turn |
| Model returns invalid JSON for analysis | One repair attempt; then a minimal summary from the transcript, with code-detected signals kept |
| Model invents a quote | The observation, memory or alert is dropped before storage |
| Model raises the plumber as an alert | Demoted to a follow-up observation; tomorrow's call asks about it |
| Model forgets the plumber entirely | The parent's line about an undelivered tradesperson or parcel is recorded as a follow-up from their own words |
| Model marks a message delivered after "I'll let Hira know" | Rejected; the message stays queued for the next call |
| Parent sounds tired | Remaining plan items skipped and a warm close, unless a family `ask` is still unanswered |
| Turn or minute budget reached | Warm close |
| No answer / busy | Call recorded; second consecutive → `today` note; third → `now` alert |
| A messenger fails for one family member | Logged; other members still receive their note |
| Twilio status callback arrives after the session ended | Ignored; the session already recorded the outcome |
| Parent rings from an unknown number | A short spoken explanation and hang-up; nothing is created |
| Relay closes without a status callback (inbound calls) | `on_end` runs the pipeline; a later callback finds the call already notified |
| Database created by an older version | Missing columns are added on open; existing rows are untouched |
| Nobody answers the morning call | One retry fifteen minutes later, then nothing until tomorrow; counted as one day |
| Someone types a stranger's number into "Start with your parent" | A household is created but never rung: the first call must be placed by a signed-in family member |
| A messaging app previews the sign-in link | The GET shows a confirm page and consumes nothing; the tap POSTs and signs in |
| Recording fails to start | Logged; the call goes on unrecorded |
| The model restates a memory it already stored | Deduplicated on text, ignoring case and spacing |

## What the live runs changed

Running against gpt-4.1-mini found what scripted tests could not. The model asked about sleep after the parent had already said how she slept; it promised to "tell Farida" (a neighbour) things; it raised the plumber as a family alert; it said "Amma" in every sentence; it marked a family "ask" as delivered after mentioning the topic in passing. Each is now fixed in code or prompt with a test: progress rules that count information volunteered by the parent, a prompt rule about who Sunday reports to, the household demotion in analysis, `_trim_address`, and the ask gate in `update_progress`.

A second end-to-end pass through three simulated calls (an ordinary day, a fall, a day with pain) found the bigger problems were in code, not the model. Emergency alerts were only sent when the call ended, while the emergency script never ends the call. The emergency number was always 911, including for a parent in Karachi. "Haven't eaten yet, I'll make something later" at breakfast raised a no-food alert. The weekly digest said "Nothing worrying" after a fall, because it only read observations. A "tell them" message was marked delivered after Sunday said "I'll let Hira know". The follow-up flag landed on a reply to a family message instead of the plumber. The closing line said "you're well" on a day with a pain alert. Each is now fixed with a test; the fixes are the `on_alert` hook, `emergency_number_for`, the trigger softeners, alerts in `compute_trends`, `message_was_said`, the follow-up guards in analysis, and `CLOSING_LINES_CONCERN`.

## Tradeoffs

- **Keyword triggers, not a classifier, for emergencies.** Deterministic, multilingual, instant, and they work when the model is down. They will miss indirect phrasing; the model classification layer for that is described in `safety.py` and not yet wired in. False positives cost a family a phone call; false negatives cost more.
- **Text in, text out via Conversation Relay** rather than streaming audio to a speech model. Simpler, cheaper, and it keeps the brain transport-agnostic so the simulator exercises the same code as a real call. The cost is Twilio's voices and transcription quality in Urdu and Hindi.
- **One model call per turn plus one bookkeeping call.** The bookkeeping call is cheap and keeps the main prompt from having to track state; its failure is harmless.
- **Household follow-ups are the model's job, alerts are code's.** The model is good at noticing "did the plumber come" is worth asking tomorrow; it is not trusted to decide what wakes a family up.
- **SQLite, one process.** Right for a handful of parents and a daily call each. Nothing in the module boundaries prevents swapping the repository.

## Where this sits on the roadmap

Phase 0 is complete: the daily call in the parent's language, memory across calls, medication and dated reminder questions, the WhatsApp or SMS note after each call, missed-call and keyword alerts, and the parent able to call in. From Phase 1: self-serve onboarding with the introductory call, the question queue, alert tiers and roles per family member, inviting siblings, the Sunday-evening weekly report, recordings with consent and the listen link, and the family page (today's note, the fortnight strip, alerts with acknowledgement, the queue, memories, medications, settings, who-hears-what, export) are done.

Not yet: a voice per parent, charts beyond the fortnight strip, the care-coordination list of appointments and doctors the call can read from, billing, story prompts and the archive, the model-based safety classifier, the B2B API. None change the shape above.
