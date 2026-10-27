"""Command line.

    sunday parent add --name "Fatima Zaidi" --phone +1555... --language ur --time 09:00 \\
        --call-me Amma --introduced-by "your son Daniyal"
    sunday family add <parent_id> --name Daniyal --phone +1555... --relation son --alerts info
    sunday med add <parent_id> "blood pressure tablet" --when morning
    sunday memory add <parent_id> "Grandchildren are Ayaan (7) and Zara (4)."
    sunday message <parent_id> --from Hira "I'll call Thursday after work."
    sunday simulate <parent_id>          talk to Sunday as the parent, in your terminal
    sunday call <parent_id>              place a real call (needs Twilio)
    sunday due                           place calls for everyone whose time it is
    sunday digest <parent_id>            this week's trends
    sunday status                        everyone at a glance
    sunday serve                         web server for Twilio + dashboard
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sunday import __version__
from sunday.app import Services
from sunday.calls.dialer import dial
from sunday.calls.transport import Simulator
from sunday.config import MissingCredentialError, load_settings
from sunday.notify.digest import compute_trends, format_digest, send_digest
from sunday.scheduler import digest_due_parents, due_parents

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Sunday: a daily phone call for your parent."
)
parent_app = typer.Typer(help="Parents we call.")
family_app = typer.Typer(help="Family members who get the notes.")
med_app = typer.Typer(help="Medications the call asks about.")
memory_app = typer.Typer(help="Things Sunday should know about the parent.")
app.add_typer(parent_app, name="parent")
app.add_typer(family_app, name="family")
app.add_typer(med_app, name="med")
app.add_typer(memory_app, name="memory")
console = Console()
err = Console(stderr=True)

DbOpt = typer.Option(None, "--db", help="SQLite path (default from SUNDAY_DB / .env)")


def _services(db: Path | None, *, need_model: bool = False) -> Services:
    from sunday.logging_setup import configure_logging

    settings = load_settings(**({"SUNDAY_DB": db} if db else {}))
    configure_logging(settings.log_level, quiet_console=True)
    try:
        return Services.from_settings(settings)
    except MissingCredentialError as exc:
        if need_model:
            _fail(str(exc))
        settings = load_settings(**({"SUNDAY_DB": db} if db else {}), SUNDAY_PROVIDER="fake")
        return Services.from_settings(settings)


def _fail(msg: str, code: int = 1) -> None:
    err.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(code)


def _parent_or_fail(svc: Services, parent_id: str):
    p = svc.repo.get_parent(parent_id)
    if p is None:
        _fail(f"no parent '{parent_id}'. Run `sunday status` to list them.")
    return p


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context, version: bool = typer.Option(False, "--version")) -> None:
    if version:
        console.print(f"sunday {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# --------------------------------------------------------------- parent / family / med / memory


@parent_app.command("add")
def parent_add(
    name: str = typer.Option(..., "--name"),
    phone: str = typer.Option(..., "--phone", help="E.164, e.g. +923001234567"),
    language: str = typer.Option("en", "--language", help="en, ur, hi, pa ..."),
    time: str = typer.Option("09:00", "--time", help="Local call time HH:MM"),
    timezone: str = typer.Option("UTC", "--timezone", help="e.g. Asia/Karachi"),
    call_me: str = typer.Option("", "--call-me", help="How to address them, e.g. Amma"),
    introduced_by: str = typer.Option("", "--introduced-by", help="e.g. 'your son Daniyal'"),
    notes: str = typer.Option("", "--notes"),
    db: Path | None = DbOpt,
) -> None:
    """Add a parent to call."""
    svc = _services(db)
    p = svc.repo.add_parent(
        name=name,
        phone=phone,
        language=language,
        call_time=time,
        timezone=timezone,
        preferred_name=call_me,
        introduced_by=introduced_by,
        notes=notes,
    )
    console.print(
        f"Added [bold]{p.name}[/bold] -> id [bold]{p.id}[/bold]. "
        f"Next: `sunday family add {p.id} --name ... --phone ...`"
    )


@family_app.command("add")
def family_add(
    parent_id: str,
    name: str = typer.Option(..., "--name"),
    phone: str = typer.Option(..., "--phone"),
    relation: str = typer.Option("", "--relation"),
    channel: str = typer.Option("whatsapp", "--channel", help="whatsapp | sms | console"),
    alerts: str = typer.Option("info", "--alerts", help="info (everything) | today | now"),
    primary: bool = typer.Option(False, "--primary"),
    db: Path | None = DbOpt,
) -> None:
    """Add a family member who receives notes and alerts."""
    svc = _services(db)
    _parent_or_fail(svc, parent_id)
    m = svc.repo.add_family_member(
        parent_id,
        name=name,
        phone=phone,
        relation=relation,
        channel=channel,
        min_alert_tier=alerts,
        is_primary=primary,
    )
    console.print(
        f"Added {m.name} ({m.relation or 'family'}), gets: {alerts} and above via {channel}."
    )


@med_app.command("add")
def med_add(
    parent_id: str,
    name: str,
    when: str = typer.Option("morning", "--when"),
    db: Path | None = DbOpt,
) -> None:
    """Add a medication the call should ask about."""
    svc = _services(db)
    p = _parent_or_fail(svc, parent_id)
    meds = list(p.medications or []) + [{"name": name, "when": when}]
    svc.repo.update_parent(parent_id, medications=meds)
    console.print(f"{p.address()} will be asked about: " + ", ".join(m["name"] for m in meds))


@memory_app.command("add")
def memory_add(
    parent_id: str, text: str, topic: str = typer.Option("", "--topic"), db: Path | None = DbOpt
) -> None:
    """Tell Sunday something about the parent (grandchildren, the knee, the neighbour)."""
    svc = _services(db)
    _parent_or_fail(svc, parent_id)
    svc.repo.add_memory(parent_id, text, topic=topic, source="family")
    console.print("Remembered.")


@app.command()
def message(
    parent_id: str,
    text: str,
    from_name: str = typer.Option(..., "--from"),
    kind: str = typer.Option("say", "--kind", help="say | ask | remind"),
    on: str | None = typer.Option(
        None,
        "--on",
        help="Hold it until this date (YYYY-MM-DD), e.g. the day before a clinic visit",
    ),
    db: Path | None = DbOpt,
) -> None:
    """Queue something for Sunday to say or ask on the next call (or on a given day)."""
    svc = _services(db)
    _parent_or_fail(svc, parent_id)
    remind_on = None
    if on:
        try:
            remind_on = date.fromisoformat(on)
        except ValueError:
            _fail(f"--on must be YYYY-MM-DD, got {on!r}")
    svc.repo.queue_message(
        parent_id, from_name=from_name, text=text, kind=kind, remind_on=remind_on
    )
    when = f"from {remind_on:%a %d %b}" if remind_on else "for the next call"
    console.print(f"Queued {when} ({kind}): {text}")


# --------------------------------------------------------------- calls


@app.command()
def simulate(parent_id: str, db: Path | None = DbOpt) -> None:
    """Have the call in your terminal, typing as the parent. No telephony needed."""
    svc = _services(db, need_model=True)
    p = _parent_or_fail(svc, parent_id)
    console.print(
        Panel(
            f"You are [bold]{p.name}[/bold]. Sunday is calling. Type what they would "
            "say; [bold]/hangup[/bold] to end.",
            title="Simulated call",
            border_style="cyan",
        )
    )
    session = svc.new_session(p)

    def ask(prompt: str) -> str:
        return typer.prompt(prompt, prompt_suffix="", default="", show_default=False)

    def show(text: str) -> None:
        console.print("[bold blue]Sunday >[/bold blue] ", end="")
        console.print(text, markup=False, highlight=False)

    def note(text: str) -> None:
        console.print(f"[red]! {text}[/red]")

    Simulator(session).run_interactive(ask, show, note)
    console.print("\n[dim]Call ended. Writing the family note...[/dim]\n")
    svc.complete(session)
    call = svc.repo.get_call(session.call.id)
    if call and call.summary:
        console.print(Panel(call.summary, title="Summary stored", border_style="green"))


@app.command()
def call(parent_id: str, db: Path | None = DbOpt) -> None:
    """Place a real phone call now (needs Twilio; `sunday serve` must be reachable)."""
    svc = _services(db, need_model=True)
    p = _parent_or_fail(svc, parent_id)
    if not svc.settings.twilio_ready():
        _fail(
            "Twilio is not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER). "
            "Use `sunday simulate` to test without telephony."
        )
    if not svc.settings.public_url:
        _fail("SUNDAY_PUBLIC_URL is not set; Twilio must be able to reach `sunday serve`.")
    call_id = dial(svc.repo, svc.telephony, svc.settings, p)
    console.print(f"Dialing {p.phone} ... call id {call_id}. Watch `sunday serve` logs.")


@app.command()
def due(db: Path | None = DbOpt, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Place calls for everyone whose call time it is, and send the weekly report on Sunday
    evening. Run every few minutes from cron."""
    svc = _services(db, need_model=not dry_run)
    parents = due_parents(svc.repo)
    digests = digest_due_parents(svc.repo)
    if not parents and not digests:
        console.print("Nobody is due right now.")
        return
    for p in parents:
        if dry_run or not svc.settings.twilio_ready():
            console.print(f"would call {p.name} ({p.phone}) now")
        else:
            console.print(f"calling {p.name}: {dial(svc.repo, svc.telephony, svc.settings, p)}")
    for p in digests:
        if dry_run:
            console.print(f"would send {p.address()}'s weekly report now")
        else:
            n = send_digest(svc.repo, svc.messenger, p)
            console.print(f"sent {p.address()}'s weekly report to {n} family member(s)")


# --------------------------------------------------------------- reporting


@app.command()
def status(db: Path | None = DbOpt) -> None:
    """Everyone at a glance."""
    svc = _services(db)
    parents = svc.repo.list_parents()
    if not parents:
        console.print("No parents yet. `sunday parent add --name ... --phone ...`")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("id", "parent", "call", "family", "last call", "open alerts"):
        table.add_column(col)
    for p in parents:
        last = svc.repo.last_completed_call(p.id)
        alerts = [a for a in svc.repo.alerts_since(p.id, days=7) if not a.acknowledged_by]
        table.add_row(
            p.id,
            p.name,
            f"{p.language} {p.call_time} {p.timezone}",
            str(len(svc.repo.family_for(p.id))),
            (last.summary[:60] + "...") if last and last.summary else "-",
            ", ".join(sorted({a.tier for a in alerts})) or "-",
        )
    console.print(table)


@app.command()
def digest(
    parent_id: str,
    days: int = typer.Option(7, "--days"),
    send: bool = typer.Option(
        False, "--send", help="Also send it to family members who get 'info'"
    ),
    db: Path | None = DbOpt,
) -> None:
    """This week's trends for one parent."""
    svc = _services(db)
    p = _parent_or_fail(svc, parent_id)
    text = format_digest(p, compute_trends(svc.repo, p, days=days))
    console.print(Panel(text, title=f"{p.address()}", border_style="yellow"))
    if send:
        n = send_digest(svc.repo, svc.messenger, p, days=days)
        console.print(f"Sent to {n} family member(s).")


@app.command()
def show(parent_id: str, db: Path | None = DbOpt) -> None:
    """Recent calls, alerts, memories and queued messages for one parent."""
    svc = _services(db)
    p = _parent_or_fail(svc, parent_id)
    console.print(
        Panel(
            f"{p.name} ({p.address()}) - {p.phone} - {p.language} at {p.call_time} "
            f"{p.timezone}\nMedications: "
            + (", ".join(m["name"] for m in p.medications) or "none")
            + "\nFamily: "
            + (", ".join(f"{m.name} ({m.relation})" for m in svc.repo.family_for(p.id)) or "none"),
            title=p.id,
        )
    )
    for c in svc.repo.recent_calls(p.id, days=14)[:7]:
        when = c.started_at or c.scheduled_for
        console.print(
            f"[bold]{when:%a %d %b %H:%M}[/bold] {c.status} {c.mood} - "
            f"{c.summary or '(no summary)'}"
        )
    alerts = svc.repo.alerts_since(p.id, days=14)
    if alerts:
        console.print("\n[bold]Alerts[/bold]")
        for a in alerts:
            console.print(f"  {a.tier.upper()}: {a.text}")
    mems = svc.repo.memories_for(p.id)
    if mems:
        console.print("\n[bold]Sunday knows[/bold]")
        for m in mems[-10:]:
            console.print(f"  - {m.text}")
    q = svc.repo.undelivered_messages(p.id, include_future=True)
    if q:
        console.print("\n[bold]Queued for next call[/bold]")
        for m in q:
            when = f" [dim](from {m.remind_on:%a %d %b})[/dim]" if m.remind_on else ""
            console.print(f"  - ({m.kind}) from {m.from_name}: {m.text}{when}")


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Loopback by default. Put a tunnel or reverse proxy in front; do not bind wider.",
    ),
    port: int = typer.Option(8000, "--port"),
    db: Path | None = DbOpt,
) -> None:
    """Run the web server: the family page and Twilio webhooks."""
    import uvicorn

    from sunday.config import MissingCredentialError
    from sunday.web import create_app

    svc = _services(db, need_model=True)
    try:
        svc.settings.require_secret_key()
    except MissingCredentialError:
        _fail(
            "SUNDAY_SECRET_KEY is not set. It signs family sign-in links and sessions. Generate "
            'one: python -c "import secrets; print(secrets.token_urlsafe(32))" and put it in .env'
        )
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]warning:[/yellow] binding to {host} makes the server reachable from other "
            "machines. Prefer 127.0.0.1 behind a tunnel or reverse proxy."
        )
    console.print(
        f"Sunday listening on http://{host}:{port}  "
        f"(public: {svc.settings.public_url or 'not set'})"
    )
    uvicorn.run(create_app(svc), host=host, port=port, log_level="info")


@app.command()
def demo(db: Path | None = DbOpt) -> None:
    """Seed a demo parent with two weeks of history, so the family page has something to show."""
    from sunday.demo import FAMILY, seed_demo

    svc = _services(db)
    pid = seed_demo(svc.repo)
    console.print(f"Demo parent ready: {pid} (Amma).")
    console.print("Sign in to the family page with one of these numbers:")
    for name, relation, phone, _tier, _primary in FAMILY:
        console.print(f"  {phone}   {name}, {relation}")
    console.print(
        "With the console messenger, the sign-in link prints in the `sunday serve` terminal."
    )


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
