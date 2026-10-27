# Architecture

One call session, fed text by whichever transport is in use, surrounded by
ordinary code that enforces every rule. The model decides what Sunday says
next and what the transcript meant; code decides when the call is an
emergency, when it ends, what the family is told and when, and what counts
as evidence.

```
cron ──► sunday due ──► dialer (Twilio) ──► parent's phone
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
| `models.py`, `repository.py` | Parent, FamilyMember, Medication, Memory, QueuedMessage, Call, Turn, Observation, Alert on local SQLite. One thin repository owns every query; nothing else touches the database. |
| `config.py` | Provider, keys, budgets, DB path, from `.env`. |
| `brain/safety.py` | Deterministic multilingual keyword triggers run on every utterance before any model. Fall, chest pain, breathing, stroke signs, bleeding, "call an ambulance", scam in progress → `now`. Severe pain, fever, confusion, low mood, medication concern, scam attempt, no food → `today`. Denials ("no pain today") and food that is on its way ("haven't eaten yet, I'll make something") keep `today` triggers quiet; a `now` trigger is never softened. `SafetyState` dedupes and tracks the highest tier. The emergency script lines and the emergency number per country (derived from the parent's phone prefix) live here too. |
| `brain/conversation.py` | A loose plan (greeting, wellbeing, sleep, medication, meals, follow-ups, family messages, open talk, close) the model weaves through. Builds the system prompt from memories, yesterday's follow-ups and queued family messages. Fixed honest opening line per language. Replies trimmed to three sentences; the parent's name allowed in at most one of every three turns. A cheap bookkeeping call marks plan items covered and messages delivered; code only accepts a message as delivered if Sunday's own words contain it. |
| `brain/analysis.py` | One structured call turns the transcript into summary, typed observations, memories and alerts, each quoting the parent. Quotes not in the transcript are dropped. Alerts whose kind or text is a household matter become follow-up observations; household observations are always follow-ups; a reply to a family message never is; a tradesperson or delivery the parent said has not come is recorded even if the model forgot it. Code-detected signals are merged in and never downgraded. Also decides the no-answer tiers. |
| `brain/model.py`, `brain/offline.py` | `ChatModel` protocol with an OpenAI adapter and a scripted model for tests; `complete_json` validates against a Pydantic model with one repair attempt. The offline brain recognises the three prompt types and answers with rules, so every path runs with no key. |
| `calls/session.py` | The live-call state machine: `start`, `hear`, `hangup`. Phases talking → emergency / closing → ended. Turn and minute budgets. Persists every turn as it happens. Hands a `now` alert to an `on_alert` hook the instant it is persisted. Never raises to the transport for model failures. |
| `calls/transport.py` | Twilio Conversation Relay: TwiML for the answered call, a WebSocket that receives final speech-to-text and sends text to speak, and the hang-up message. The terminal simulator drives the same session. |
| `calls/dialer.py`, `scheduler.py` | `Telephony` protocol with Twilio and fake implementations. `is_due` checks local call time within a 20-minute window, call days, and whether we already called today. `record_outcome` maps Twilio status callbacks and escalates no-answers: two in a row → `today`, three → `now`. |
| `notify/pipeline.py` | `notify_urgent`, called from inside a live call to send `now` alerts at once; and `finish_call`, the one function transports call when a call ends. Idempotent. Any unsent emergencies first, then analysis, storage, and a short note to each family member whose alert level wants it. |
| `notify/messenger.py` | WhatsApp or SMS via Twilio, or the console. One member's failure never blocks the others. |
| `notify/digest.py` | Trends over stored alerts and observations: the week's alerts first, then calls, medication, mood, sleep, "worth a look" flags, open follow-ups. Pure code, never a diagnosis. |
| `web.py`, `cli.py` | Twilio webhooks (`/twilio/voice`, `/twilio/relay`, `/twilio/status`), a read-only dashboard, `/health`; the operator CLI. |

## Three rules, enforced in code

1. **The parent never touches technology.** The phone rings; they answer. Twilio does speech both ways. There is no app, no login, nothing to charge or update.
2. **Safety does not depend on the model.** Keyword triggers run first on every utterance. A `now` signal persists the alert, sends it to the family through `on_alert` before Sunday says another word, switches the session to scripted lines in the parent's language with the right emergency number for their country, and keeps the line open; the model is never consulted during an emergency. The emergency script never hangs up, which is exactly why the alert cannot wait for the end of the call. If sending fails the call carries on and `finish_call` retries before its own model call. A `today` signal is recorded and the call continues normally. If the model is down the call ends with a fixed kind line and the transcript is kept.
3. **Nothing is invented.** Every observation, alert and memory must carry `evidence` that appears in the transcript; the rest are dropped before storage. The family summary is written from what survived. The opening line is fixed so Sunday always says it is an assistant sent by the family. The prompt forbids medical advice, dosages and asking for money, and the analysis step cannot produce them because it only has observation types.

## The call, turn by turn

1. Transport reports the call answered. `start` writes the opening line, marks `greeting` done.
2. Each final utterance goes to `hear`: it is persisted, then run through `keyword_signals`. Fresh signals are persisted as alerts at once; a `now` alert is also sent to the family right there, via the `on_alert` hook `Services` wires to the messenger.
3. If any `now` signal is active, the next scripted emergency line is returned and the call never closes on its own.
4. Otherwise budgets are checked (turns, minutes), then `update_progress` asks the model which plan items the parent has now covered and which family messages were delivered. Code applies three gates: an item is only marked if it is on the plan; a message is only delivered if at least half its content words appear in something Sunday actually said ("I'll let Hira know" does not deliver Hira's message); and an unanswered family `ask` keeps `family_messages` open even if the parent sounds tired.
5. If only `close` remains, the localised closing line is returned and the call ends; if anything was flagged this call the close says "how you're doing today" rather than "you're well". Otherwise `next_line` builds the system prompt (context, plan, what is done, pending asks, minutes so far) and gets one short reply, which is trimmed to three sentences and stripped of repeated use of the parent's name.
6. When the transport reports the line dropped or the session ends, `finish_call` runs the after-call pipeline.

## Context engineering

The model sees a small, fixed-shape prompt every turn: who the parent is, how to speak, the hard rules, memories, yesterday's follow-ups, pending family messages with ids and kind (`Tell them` / `ASK them and wait for their answer` / `Remind them`), the plan, what is covered, and the last 24 transcript turns. Family messages are placed early with an explicit instruction to deliver them right after asking how they are; when an `ask` is pending a one-line nudge is appended so it gets asked plainly.

The analysis prompt is given the whole transcript and the delivered-message ids and must return typed JSON. Alert tiers are defined in the prompt in terms of the person's wellbeing, with household matters explicitly excluded; code then re-checks every alert's kind and text and demotes household ones anyway.

## Persistence

SQLite via SQLAlchemy at `SUNDAY_DB` (default `data/sunday.sqlite`, gitignored; conversations with a parent are sensitive and never leave the machine unless sent to the family). Every parent turn and Sunday turn is written as it happens, so a dropped call still leaves a transcript. Alerts raised during a call are persisted before anything is said back. `finish_call` is idempotent: a call that has already been notified is skipped, so a retry after a crash is safe. Family notes record what was sent to whom.

## Failure modes

| Situation | Behaviour |
|---|---|
| Parent says a `now` trigger phrase | Alert persisted and sent to everyone before the next line is spoken; scripted calm lines with the country's emergency number; line stays open; model not consulted |
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

## What the live runs changed

Running against gpt-4.1-mini found what scripted tests could not. The model asked about sleep after the parent had already said how she slept; it promised to "tell Farida" (a neighbour) things; it raised the plumber as a family alert; it said "Amma" in every sentence; it marked a family "ask" as delivered after mentioning the topic in passing. Each is now fixed in code or prompt with a test: progress rules that count information volunteered by the parent, a prompt rule about who Sunday reports to, the household demotion in analysis, `_trim_address`, and the ask gate in `update_progress`.

A second end-to-end pass through three simulated calls (an ordinary day, a fall, a day with pain) found the bigger problems were in code, not the model. Emergency alerts were only sent when the call ended, while the emergency script never ends the call. The emergency number was always 911, including for a parent in Karachi. "Haven't eaten yet, I'll make something later" at breakfast raised a no-food alert. The weekly digest said "Nothing worrying" after a fall, because it only read observations. A "tell them" message was marked delivered after Sunday said "I'll let Hira know". The follow-up flag landed on a reply to a family message instead of the plumber. The closing line said "you're well" on a day with a pain alert. Each is now fixed with a test; the fixes are the `on_alert` hook, `emergency_number_for`, the trigger softeners, alerts in `compute_trends`, `message_was_said`, the follow-up guards in analysis, and `CLOSING_LINES_CONCERN`.

## Tradeoffs

- **Keyword triggers, not a classifier, for emergencies.** Deterministic, multilingual, instant, and they work when the model is down. They will miss indirect phrasing; the model classification layer for that is described in `safety.py` and not yet wired in. False positives cost a family a phone call; false negatives cost more.
- **Text in, text out via Conversation Relay** rather than streaming audio to a speech model. Simpler, cheaper, and it keeps the brain transport-agnostic so the simulator exercises the same code as a real call. The cost is Twilio's voices and transcription quality in Urdu and Hindi.
- **One model call per turn plus one bookkeeping call.** The bookkeeping call is cheap and keeps the main prompt from having to track state; its failure is harmless.
- **Household follow-ups are the model's job, alerts are code's.** The model is good at noticing "did the plumber come" is worth asking tomorrow; it is not trusted to decide what wakes a family up.
- **SQLite, one process.** Right for a handful of parents and a daily call each. Nothing in the module boundaries prevents swapping the repository.

## Not in this phase

Recordings, a voice per parent, inbound calls from the parent, story prompts and the yearly book, the model-based safety classifier, encrypted executor access. All are planned and none change the shape above.
