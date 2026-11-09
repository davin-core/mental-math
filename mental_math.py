import json
import logging
import math
import sqlite3
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


DEFAULT_ROUND_SECONDS = 120
ROUND_LENGTHS = (120, 600)
OPERAND_DIGIT_LENGTHS = tuple(range(1, 7))
DATABASE_PATH = Path(__file__).with_name("mental_math.sqlite3")
METHOD_TABLES = {
    "squares": "square_ending_in_5",
    "complements": "same_tens_complements",
    "eleven-two": "multiply_two_digits_by_11",
    "eleven-three": "multiply_three_digits_by_11",
    "addition": "addition",
    "single-digit-multiplication": "single_digit_multiplication",
}


def initialize_database() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        for table_name in METHOD_TABLES.values():
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    id INTEGER PRIMARY KEY,
                    completed_at TEXT NOT NULL,
                    duration_limit_seconds INTEGER NOT NULL DEFAULT 120,
                    duration_seconds REAL NOT NULL,
                    attempted INTEGER NOT NULL,
                    correct INTEGER NOT NULL,
                    incorrect INTEGER NOT NULL,
                    accuracy REAL NOT NULL,
                    average_answer_seconds REAL NOT NULL,
                    best_streak INTEGER NOT NULL,
                    operand_digits INTEGER
                )
                """
            )
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if "duration_limit_seconds" not in columns:
                connection.execute(
                    f"""ALTER TABLE {table_name}
                    ADD COLUMN duration_limit_seconds INTEGER NOT NULL DEFAULT 120"""
                )
            if "operand_digits" not in columns:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN operand_digits INTEGER"
                )
            if "first_operand_digits" not in columns:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN first_operand_digits INTEGER"
                )
            if "second_operand_digits" not in columns:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN second_operand_digits INTEGER"
                )
            if table_name == METHOD_TABLES["addition"]:
                connection.execute(
                    """
                    UPDATE addition
                    SET first_operand_digits = operand_digits,
                        second_operand_digits = operand_digits
                    WHERE operand_digits IS NOT NULL
                      AND first_operand_digits IS NULL
                      AND second_operand_digits IS NULL
                    """
                )


def save_round(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("Round data must be a JSON object.")

    mode = data.get("mode")
    if not isinstance(mode, str) or mode not in METHOD_TABLES:
        raise ValueError("Unknown quiz method.")

    integer_fields = ("attempted", "correct", "incorrect", "best_streak")
    values: dict[str, int] = {}
    for field in integer_fields:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer.")
        values[field] = value

    if values["correct"] + values["incorrect"] != values["attempted"]:
        raise ValueError("Correct and incorrect counts must equal attempted.")
    if values["best_streak"] > values["correct"]:
        raise ValueError("Best streak cannot exceed the number correct.")

    round_limit = data.get("duration_limit_seconds")
    if (
        isinstance(round_limit, bool)
        or not isinstance(round_limit, int)
        or round_limit not in ROUND_LENGTHS
    ):
        raise ValueError("Round length must be 120 or 600 seconds.")

    first_operand_digits = data.get("first_operand_digits")
    second_operand_digits = data.get("second_operand_digits")
    if mode in ("addition", "single-digit-multiplication"):
        legacy_digits = data.get("operand_digits")
        if (
            mode == "addition"
            and first_operand_digits is None
            and second_operand_digits is None
        ):
            first_operand_digits = second_operand_digits = legacy_digits
        if (
            isinstance(first_operand_digits, bool)
            or not isinstance(first_operand_digits, int)
            or first_operand_digits not in OPERAND_DIGIT_LENGTHS
            or isinstance(second_operand_digits, bool)
            or not isinstance(second_operand_digits, int)
            or second_operand_digits not in OPERAND_DIGIT_LENGTHS
        ):
            raise ValueError("Each operand must have between 1 and 6 digits.")
    elif (
        first_operand_digits is not None
        or second_operand_digits is not None
        or data.get("operand_digits") is not None
    ):
        raise ValueError("Digit length only applies to addition rounds.")

    duration = data.get("duration_seconds")
    average = data.get("average_answer_seconds")
    for name, value in (("duration_seconds", duration), ("average_answer_seconds", average)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number.")
    if duration > round_limit:
        raise ValueError("Round duration cannot exceed the round length.")
    if average > duration:
        raise ValueError("Average answer time cannot exceed the round duration.")

    attempted = values["attempted"]
    accuracy = values["correct"] / attempted * 100 if attempted else 0.0
    table_name = METHOD_TABLES[mode]
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            f"""
            INSERT INTO {table_name} (
                completed_at, duration_limit_seconds, duration_seconds, attempted, correct, incorrect,
                accuracy, average_answer_seconds, best_streak, operand_digits,
                first_operand_digits, second_operand_digits
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                round_limit,
                duration,
                attempted,
                values["correct"],
                values["incorrect"],
                accuracy,
                average,
                values["best_streak"],
                first_operand_digits if first_operand_digits == second_operand_digits else None,
                first_operand_digits,
                second_operand_digits,
            ),
        )


def load_history(
    mode: str,
    limit: int = 100,
    duration_limit_seconds: int | None = None,
    first_operand_digits: int | None = None,
    second_operand_digits: int | None = None,
) -> list[dict[str, object]]:
    if mode not in METHOD_TABLES:
        raise ValueError("Unknown quiz method.")
    if duration_limit_seconds not in (None, *ROUND_LENGTHS):
        raise ValueError("Round length must be 120 or 600 seconds.")
    for operand_digits in (first_operand_digits, second_operand_digits):
        if operand_digits is not None and (
            isinstance(operand_digits, bool)
            or not isinstance(operand_digits, int)
            or mode not in ("addition", "single-digit-multiplication")
            or operand_digits not in OPERAND_DIGIT_LENGTHS
        ):
            raise ValueError("Digit filters are only available for addition or multiplication (1 to 6 digits).")

    table_name = METHOD_TABLES[mode]
    filters: list[str] = []
    parameters: list[int] = []
    if duration_limit_seconds is not None:
        filters.append("duration_limit_seconds = ?")
        parameters.append(duration_limit_seconds)
    if first_operand_digits is not None:
        filters.append("first_operand_digits = ?")
        parameters.append(first_operand_digits)
    if second_operand_digits is not None:
        filters.append("second_operand_digits = ?")
        parameters.append(second_operand_digits)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    parameters.append(limit)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT id, completed_at, duration_limit_seconds, duration_seconds, attempted, correct, incorrect,
                   accuracy, average_answer_seconds, best_streak, operand_digits,
                   first_operand_digits, second_operand_digits
            FROM (
                SELECT id, completed_at, duration_limit_seconds, duration_seconds, attempted, correct, incorrect,
                       accuracy, average_answer_seconds, best_streak, operand_digits,
                       first_operand_digits, second_operand_digits
                FROM {table_name}
                {where_clause}
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mental Math Sprint</title>
  <style>
    :root {
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1b2340;
      background: #f5f6fb;
      --ink: #1b2340;
      --muted: #78809a;
      --primary: #6254d9;
      --primary-dark: #4b3dc1;
      --line: #e8eaf2;
      --surface: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      min-height: 100vh;
      margin: 0;
      padding: 44px 24px 64px;
      background:
        radial-gradient(ellipse at 12% 0%, #e8e5ff 0, transparent 34rem),
        radial-gradient(ellipse at 100% 20%, #e2f4f3 0, transparent 27rem),
        #f5f6fb;
    }
    main { width: min(100%, 980px); margin: 0 auto; }
    .hero { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; margin-bottom: 28px; }
    .eyebrow { display: inline-flex; align-items: center; gap: 8px; margin: 0 0 12px; color: var(--primary); font-size: 0.76rem; font-weight: 800; letter-spacing: 0.13em; }
    .eyebrow-dot { width: 8px; height: 8px; border-radius: 50%; background: #38ad89; box-shadow: 0 0 0 4px #38ad8920; }
    h1 { margin: 0; color: var(--ink); font-size: clamp(2rem, 5vw, 3rem); letter-spacing: -0.045em; line-height: 1.08; }
    .subtitle { margin: 10px 0 0; color: var(--muted); font-size: 1.03rem; }
    .round-badge { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; padding: 12px 16px; border: 1px solid #e8e7f2; border-radius: 14px; background: #ffffffbb; color: #5b627c; font-size: 0.88rem; font-weight: 700; }
    .badge-icon { display: grid; width: 30px; height: 30px; place-items: center; border-radius: 9px; background: #eeecff; color: var(--primary); font-size: 1rem; }
    .practice-layout { display: block; }
    .mode-card, .card, .stat, .chart-card, .table-wrap { border: 1px solid #eceef5; background: var(--surface); box-shadow: 0 12px 34px #25244b0a; }
    .mode-card { display: grid; grid-template-columns: minmax(180px, 0.7fr) minmax(0, 1.3fr); align-items: center; gap: 22px; margin-bottom: 18px; padding: 18px 22px; border-radius: 18px; text-align: left; }
    .section-kicker { margin: 0 0 6px; color: #a0a5b8; font-size: 0.72rem; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; }
    .section-title { margin: 0; color: var(--ink); font-size: 1.12rem; letter-spacing: -0.02em; }
    .mode-picker { text-align: left; }
    .operand-digits-picker { display: grid; grid-template-columns: 1fr auto 1fr; align-items: end; gap: 10px; }
    #operand-digits-picker { grid-column: 1 / -1; }
    .addition-plus { padding-bottom: 12px; color: var(--primary); font-size: 1.2rem; font-weight: 800; }
    .select-grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(150px, 0.7fr); gap: 12px; }
    .mode-picker label { display: block; margin-bottom: 6px; color: #858ba1; font-size: 0.7rem; font-weight: 800; letter-spacing: 0.08em; }
    #mode, #round-length {
      width: 100%;
      padding: 12px 40px 12px 13px;
      border: 1px solid #dfe2ed;
      border-radius: 12px;
      background: #fbfbfe;
      color: var(--ink);
      font: inherit;
      font-size: 0.91rem;
      cursor: pointer;
    }
    #mode:focus-visible, #round-length:focus-visible, #answer:focus-visible { outline: 3px solid #6254d933; border-color: var(--primary); }
    #method { margin: 12px 0 0; padding: 11px 14px; border: 1px solid #eceafb; border-radius: 11px; background: #f8f7ff; color: #606782; font-size: 0.83rem; line-height: 1.5; }
    .card { display: flex; flex-direction: column; justify-content: center; min-height: 460px; padding: 36px; border-radius: 22px; text-align: center; }
    #timer { color: var(--primary); font-size: 1.05rem; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: 0.08em; }
    #question { display: grid; min-height: 170px; place-items: center; margin: 8px 0 18px; color: var(--ink); font-size: clamp(3.5rem, 10vw, 6.5rem); font-weight: 800; letter-spacing: -0.055em; line-height: 1.05; }
    #answer-form { display: flex; flex-direction: column; align-items: center; }
    #answer {
      width: min(100%, 340px);
      padding: 16px;
      border: 1px solid #dfe2ed;
      border-radius: 14px;
      background: #fbfbfe;
      color: var(--ink);
      font: inherit;
      font-size: 1.55rem;
      text-align: center;
    }
    #feedback { min-height: 42px; margin: 12px 0 0; color: var(--muted); font-size: 0.9rem; }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 18px 0 24px; }
    .stat { padding: 18px; border-radius: 16px; text-align: left; }
    .stat-label { color: #858ba1; font-size: 0.7rem; font-weight: 800; letter-spacing: 0.09em; }
    .stat-value { display: block; margin-top: 7px; color: var(--ink); font-size: 1.6rem; font-weight: 780; letter-spacing: -0.04em; }
    .actions { display: flex; justify-content: center; flex-wrap: wrap; gap: 12px; }
    button {
      min-width: 170px;
      padding: 13px 22px;
      border: 0;
      border-radius: 12px;
      background: var(--primary);
      color: white;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
      transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }
    button:hover { transform: translateY(-1px); background: var(--primary-dark); box-shadow: 0 7px 16px #6254d933; }
    button:active { transform: translateY(0); }
    button:disabled { opacity: 0.65; cursor: wait; transform: none; box-shadow: none; }
    button.secondary { border: 1px solid #e1e2ed; background: #ffffffa8; color: #545a77; }
    button.secondary:hover { background: white; box-shadow: 0 6px 16px #25244b0c; }
    button:focus-visible { outline: 3px solid #6254d955; outline-offset: 3px; }
    .hint { margin: 13px 0 0; color: #9298aa; font-size: 0.82rem; text-align: center; }
    #history { margin-top: 48px; text-align: left; }
    .history-heading { display: flex; justify-content: space-between; align-items: end; gap: 16px; margin-bottom: 8px; }
    #history h2 { margin: 0; color: var(--ink); font-size: 1.65rem; letter-spacing: -0.035em; }
    #history-status { min-height: 24px; margin: 8px 0 18px; color: var(--muted); font-size: 0.9rem; }
    .chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    .chart-card { min-width: 0; padding: 20px; border-radius: 18px; }
    .chart-card h3 { margin: 0 0 16px; color: #454c67; font-size: 0.95rem; }
    .chart-frame { position: relative; width: 100%; height: 240px; }
    .history-filter { width: min(100%, 240px); padding: 10px 12px; border: 1px solid #dfe2ed; border-radius: 10px; background: white; color: var(--ink); font: inherit; }
    .history-toolbar { display: flex; justify-content: flex-start; align-items: center; flex-wrap: wrap; gap: 12px 24px; margin: 10px 0 14px; }
    .history-toolbar label { color: #858ba1; font-size: 0.74rem; font-weight: 800; letter-spacing: 0.06em; }
    .history-filter-wrap { display: flex; align-items: center; gap: 10px; }
    .history-filter:focus-visible { outline: 3px solid #6254d933; border-color: var(--primary); }
    .table-wrap { margin-top: 16px; overflow-x: auto; border-radius: 16px; }
    table { width: 100%; border-collapse: collapse; text-align: right; white-space: nowrap; }
    th, td { padding: 13px 16px; border-bottom: 1px solid #eff0f5; }
    th { background: #fafaff; color: #858ba1; font-size: 0.72rem; font-weight: 800; letter-spacing: 0.07em; text-transform: uppercase; }
    th:first-child, td:first-child { text-align: left; }
    td { color: #4d546e; font-size: 0.88rem; }
    tbody tr:hover { background: #fafaff; }
    tbody tr:last-child td { border-bottom: 0; }
    @media (max-width: 720px) {
      body { padding: 28px 16px 48px; }
      .hero { align-items: flex-start; flex-direction: column; gap: 16px; }
      .mode-card { grid-template-columns: 1fr; gap: 12px; padding: 18px; }
      .card { min-height: 360px; padding: 26px 18px; }
      #question { min-height: 130px; }
      .chart-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 480px) {
      .select-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 420px) {
      .stats { gap: 8px; }
      .stat { padding: 13px 11px; }
      .stat-value { font-size: 1.35rem; }
      .actions { display: grid; grid-template-columns: 1fr; }
      .actions button { width: 100%; }
      .chart-frame { height: 210px; }
    }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <main>
    <header class="hero">
      <div>
        <p class="eyebrow"><span class="eyebrow-dot"></span>MENTAL MATH SPRINT</p>
        <h1>Make every minute count.</h1>
        <p class="subtitle">Build speed and confidence, one round at a time.</p>
      </div>
      <div class="round-badge"><span class="badge-icon">◷</span><span id="round-label">2-minute sprint</span></div>
    </header>
    <section class="practice-layout" aria-label="Practice round">
      <div class="mode-card">
        <div>
          <p class="section-kicker">Your practice</p>
          <h2 class="section-title">Choose a number trick</h2>
        </div>
        <div class="mode-picker">
          <div class="select-grid">
            <div>
              <label for="mode">PRACTICE MODE</label>
              <select id="mode">
                <option value="squares">Square a number ending in 5 (e.g. 35²)</option>
                <option value="complements">Same tens digit; ones digits sum to 10 (e.g. 43 × 47)</option>
                <option value="eleven-two">Multiply a two-digit number by 11</option>
                <option value="eleven-three">Multiply a three-digit number by 11</option>
                <option value="addition">Addition</option>
                <option value="single-digit-multiplication">Multiplication</option>
              </select>
            </div>
            <div>
              <label for="round-length">ROUND LENGTH</label>
              <select id="round-length">
                <option value="120">2 minutes · Sprint</option>
                <option value="600">10 minutes · Endurance</option>
              </select>
            </div>
            <div id="operand-digits-picker" class="operand-digits-picker" hidden>
              <div>
                <label id="first-operand-label" for="first-operand-digits">FIRST ADDEND</label>
                <select id="first-operand-digits">
                  <option value="1">1 digit</option>
                  <option value="2" selected>2 digits</option>
                  <option value="3">3 digits</option>
                  <option value="4">4 digits</option>
                  <option value="5">5 digits</option>
                  <option value="6">6 digits</option>
                </select>
              </div>
              <span id="operand-operator" class="addition-plus" aria-hidden="true">+</span>
              <div>
                <label id="second-operand-label" for="second-operand-digits">SECOND ADDEND</label>
                <select id="second-operand-digits">
                  <option value="1" selected>1 digit</option>
                  <option value="2">2 digits</option>
                  <option value="3">3 digits</option>
                  <option value="4">4 digits</option>
                  <option value="5">5 digits</option>
                  <option value="6">6 digits</option>
                </select>
              </div>
            </div>
          </div>
          <p id="method">Multiply the digits before 5 by the next number, then append 25. Example: 35² → 3 × 4, then 25 = 1225.</p>
        </div>
      </div>
      <section class="card" aria-label="Quiz">
        <div id="timer" role="timer" aria-live="off">02:00</div>
        <div id="question" aria-live="polite">Ready?</div>
        <form id="answer-form">
          <input id="answer" type="number" inputmode="numeric" autocomplete="off"
            placeholder="Type your answer" aria-label="Your answer" disabled>
          <p id="feedback" aria-live="polite">Choose a trick, then start when you're ready.</p>
          <button id="submit" type="submit" hidden>Submit answer</button>
        </form>
      </section>
    </section>
    <section class="stats" aria-label="Round stats">
      <div class="stat"><span class="stat-label">QUESTIONS ANSWERED</span><strong id="answered" class="stat-value">0</strong></div>
      <div class="stat"><span class="stat-label">CORRECT ANSWERS</span><strong id="correct" class="stat-value">0</strong></div>
      <div class="stat"><span class="stat-label">ROUND ACCURACY</span><strong id="accuracy" class="stat-value">0%</strong></div>
    </section>
    <div class="actions">
      <button id="start" type="button">Start round</button>
      <button id="history-toggle" class="secondary" type="button" aria-expanded="false" aria-controls="history">▤ &nbsp; View history</button>
    </div>
    <p class="hint">Press Enter to submit · End a round early whenever you like</p>
    <section id="history" aria-labelledby="history-heading" hidden>
      <div class="history-heading">
        <div>
          <p class="section-kicker">YOUR PROGRESS</p>
          <h2 id="history-heading">Performance history</h2>
        </div>
      </div>
      <p id="history-status" role="status">Choose a trick to view its saved rounds.</p>
      <div class="history-toolbar">
        <div class="history-filter-wrap">
          <label for="history-duration">ROUND LENGTH</label>
          <select id="history-duration" class="history-filter">
            <option value="all">All round lengths</option>
            <option value="120">2 minutes · Sprint</option>
            <option value="600">10 minutes · Endurance</option>
          </select>
        </div>
        <div id="history-first-filter-wrap" class="history-filter-wrap" hidden>
          <label id="history-first-label" for="history-first-digits">FIRST ADDEND</label>
          <select id="history-first-digits" class="history-filter">
            <option value="all">Any digits</option>
            <option value="1">1 digit</option><option value="2">2 digits</option>
            <option value="3">3 digits</option><option value="4">4 digits</option>
            <option value="5">5 digits</option><option value="6">6 digits</option>
          </select>
        </div>
        <div id="history-second-filter-wrap" class="history-filter-wrap" hidden>
          <label id="history-second-label" for="history-second-digits">SECOND ADDEND</label>
          <select id="history-second-digits" class="history-filter">
            <option value="all">Any digits</option>
            <option value="1">1 digit</option><option value="2">2 digits</option>
            <option value="3">3 digits</option><option value="4">4 digits</option>
            <option value="5">5 digits</option><option value="6">6 digits</option>
          </select>
        </div>
      </div>
      <div id="history-content" hidden>
        <div id="history-charts" class="chart-grid">
          <div class="chart-card">
            <h3>Accuracy by round</h3>
            <div class="chart-frame"><canvas id="accuracy-chart" role="img"></canvas></div>
          </div>
          <div class="chart-card">
            <h3>Correct answers by round</h3>
            <div class="chart-frame"><canvas id="correct-chart" role="img"></canvas></div>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Date</th><th>Round</th><th>Operands</th><th>Answered</th><th>Correct</th><th>Accuracy</th><th>Avg. time</th><th>Best streak</th></tr>
            </thead>
            <tbody id="history-rows"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <script>
    let roundSeconds = __DEFAULT_ROUND_SECONDS__;
    const squares = [15, 25, 35, 45, 55, 65, 75, 85, 95];
    const timer = document.querySelector("#timer");
    const question = document.querySelector("#question");
    const answer = document.querySelector("#answer");
    const feedback = document.querySelector("#feedback");
    const form = document.querySelector("#answer-form");
    const submit = document.querySelector("#submit");
    const start = document.querySelector("#start");
    const mode = document.querySelector("#mode");
    const subtitle = document.querySelector(".subtitle");
    const method = document.querySelector("#method");
    const roundLength = document.querySelector("#round-length");
    const roundLabel = document.querySelector("#round-label");
    const operandDigitsPicker = document.querySelector("#operand-digits-picker");
    const firstOperandDigits = document.querySelector("#first-operand-digits");
    const secondOperandDigits = document.querySelector("#second-operand-digits");
    const firstOperandLabel = document.querySelector("#first-operand-label");
    const secondOperandLabel = document.querySelector("#second-operand-label");
    const operandOperator = document.querySelector("#operand-operator");
    const historyToggle = document.querySelector("#history-toggle");
    const historyPanel = document.querySelector("#history");
    const historyStatus = document.querySelector("#history-status");
    const historyContent = document.querySelector("#history-content");
    const historyCharts = document.querySelector("#history-charts");
    const historyDuration = document.querySelector("#history-duration");
    const historyFirstDigits = document.querySelector("#history-first-digits");
    const historySecondDigits = document.querySelector("#history-second-digits");
    const historyFirstFilterWrap = document.querySelector("#history-first-filter-wrap");
    const historySecondFilterWrap = document.querySelector("#history-second-filter-wrap");
    const historyFirstLabel = document.querySelector("#history-first-label");
    const historySecondLabel = document.querySelector("#history-second-label");
    const accuracyChartTitle = document.querySelector("#accuracy-chart").closest(".chart-card").querySelector("h3");
    const correctChartTitle = document.querySelector("#correct-chart").closest(".chart-card").querySelector("h3");
    let historyRequestId = 0;
    let running = false;
    let deadline = 0;
    let questionStarted = 0;
    let first = 0;
    let second = 0;
    let attempted = 0;
    let correct = 0;
    let incorrect = 0;
    let streak = 0;
    let bestStreak = 0;
    let answerTimeTotal = 0;
    let roundStartedAt = 0;
    let timerInterval = null;

    function usesOperandDigits(selectedMode) {
      return selectedMode === "addition" || selectedMode === "single-digit-multiplication";
    }

    function updateStats() {
      document.querySelector("#answered").textContent = attempted;
      document.querySelector("#correct").textContent = correct;
      document.querySelector("#accuracy").textContent =
        `${attempted ? Math.round(correct / attempted * 100) : 0}%`;
    }

    function nextQuestion() {
      if (usesOperandDigits(mode.value)) {
        const firstDigits = Number(firstOperandDigits.value);
        const secondDigits = Number(secondOperandDigits.value);
        const firstMinimum = firstDigits === 1 ? 1 : 10 ** (firstDigits - 1);
        const secondMinimum = secondDigits === 1 ? 1 : 10 ** (secondDigits - 1);
        const firstMaximum = 10 ** firstDigits - 1;
        const secondMaximum = 10 ** secondDigits - 1;
        first = Math.floor(Math.random() * (firstMaximum - firstMinimum + 1)) + firstMinimum;
        second = Math.floor(Math.random() * (secondMaximum - secondMinimum + 1)) + secondMinimum;
        const operator = mode.value === "addition" ? "+" : "×";
        question.textContent = `${first.toLocaleString()} ${operator} ${second.toLocaleString()}`;
      } else if (mode.value === "squares") {
        first = squares[Math.floor(Math.random() * squares.length)];
        second = first;
        question.textContent = `${first}²`;
      } else {
        if (mode.value === "complements") {
          const tens = Math.floor(Math.random() * 9) + 1;
          const ones = Math.floor(Math.random() * 9) + 1;
          first = tens * 10 + ones;
          second = tens * 10 + (10 - ones);
        } else {
          const digits = mode.value === "eleven-two" ? 2 : 3;
          first = digits === 2
            ? Math.floor(Math.random() * 90) + 10
            : Math.floor(Math.random() * 900) + 100;
          second = 11;
        }
        question.textContent = `${first} × ${second}`;
      }
      questionStarted = performance.now();
    }

    async function finishRound() {
      if (!running) return;
      running = false;
      clearInterval(timerInterval);
      timer.textContent = "00:00";
      answer.disabled = true;
      submit.hidden = true;
      mode.disabled = false;
      roundLength.disabled = false;
      firstOperandDigits.disabled = false;
      secondOperandDigits.disabled = false;
      start.textContent = "Start another round";
      start.disabled = true;
      const accuracy = attempted ? Math.round(correct / attempted * 100) : 0;
      const average = attempted ? (answerTimeTotal / attempted / 1000).toFixed(1) : "0.0";
      feedback.textContent =
        `Round complete! ${incorrect} incorrect · ${accuracy}% accuracy · ` +
        `${average}s average · best streak: ${bestStreak}. Saving stats...`;
      feedback.style.color = "#18233b";

      const roundData = {
        mode: mode.value,
        first_operand_digits: usesOperandDigits(mode.value) ? Number(firstOperandDigits.value) : null,
        second_operand_digits: usesOperandDigits(mode.value) ? Number(secondOperandDigits.value) : null,
        duration_limit_seconds: roundSeconds,
        duration_seconds: Math.min(roundSeconds, (performance.now() - roundStartedAt) / 1000),
        attempted,
        correct,
        incorrect,
        average_answer_seconds: attempted ? answerTimeTotal / attempted / 1000 : 0,
        best_streak: bestStreak
      };
      try {
        const response = await fetch("/save-round", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(roundData)
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "The server could not save this round.");
        feedback.textContent =
          `Round complete! ${incorrect} incorrect · ${accuracy}% accuracy · ` +
          `${average}s average · best streak: ${bestStreak}. Saved to SQLite.`;
        if (!historyPanel.hidden) loadHistory();
      } catch (error) {
        console.error("Could not save round stats:", error);
        const message = error instanceof Error ? error.message : "Unknown save error.";
        feedback.textContent =
          `Round complete, but stats were not saved: ${message}`;
        feedback.style.color = "#c04b38";
      } finally {
        start.disabled = false;
      }
    }

    function updateTimer() {
      const remaining = Math.max(0, deadline - performance.now());
      const seconds = Math.ceil(remaining / 1000);
      timer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:` +
        `${String(seconds % 60).padStart(2, "0")}`;
      if (remaining <= 0) finishRound();
    }

    function startRound() {
      attempted = correct = incorrect = streak = bestStreak = answerTimeTotal = 0;
      running = true;
      roundSeconds = Number(roundLength.value);
      roundStartedAt = performance.now();
      deadline = performance.now() + roundSeconds * 1000;
      start.textContent = "End round";
      mode.disabled = true;
      roundLength.disabled = true;
      firstOperandDigits.disabled = true;
      secondOperandDigits.disabled = true;
      answer.disabled = false;
      submit.hidden = false;
      answer.value = "";
      feedback.textContent = "Good luck!";
      feedback.style.color = "#63708a";
      updateStats();
      nextQuestion();
      answer.focus();
      updateTimer();
      timerInterval = setInterval(updateTimer, 100);
    }

    let accuracyChart = null;
    let correctChart = null;

    function drawChart(canvas, previousChart, records, field, maximum, isBarChart) {
      if (previousChart) previousChart.destroy();
      const color = isBarChart ? "#28a88a" : "#6254d9";
      const fill = isBarChart ? "rgba(40, 168, 138, 0.72)" : "rgba(98, 84, 217, 0.11)";
      const labels = records.map((_record, index) => `Round ${index + 1}`);
      return new Chart(canvas, {
        type: isBarChart ? "bar" : "line",
        data: {
          labels,
          datasets: [{
            data: records.map(record => Number(record[field])),
            borderColor: color,
            backgroundColor: fill,
            borderWidth: 2.5,
            borderRadius: isBarChart ? 5 : 0,
            borderSkipped: false,
            fill: !isBarChart,
            tension: 0.35,
            pointRadius: records.length > 30 ? 0 : 3.5,
            pointHoverRadius: 6,
            pointBackgroundColor: "#ffffff",
            pointBorderColor: color,
            pointBorderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 450 },
          interaction: { intersect: false, mode: "index" },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "#24233e",
              padding: 11,
              displayColors: false,
              callbacks: {
                title: items => {
                  const record = records[items[0].dataIndex];
                  return `Round ${items[0].dataIndex + 1} · ${new Date(record.completed_at).toLocaleString()}`;
                },
                label: item => field === "accuracy"
                  ? `Accuracy: ${Number(item.raw).toFixed(0)}%`
                  : `Correct: ${item.raw}`
              }
            }
          },
          scales: {
            x: {
              grid: { display: false },
              border: { display: false },
              ticks: {
                color: "#9298aa",
                maxTicksLimit: 6,
                maxRotation: 0,
                autoSkip: true,
                font: { size: 11 }
              }
            },
            y: {
              beginAtZero: true,
              min: 0,
              max: maximum,
              border: { display: false, dash: [4, 4] },
              grid: { color: "#eff0f5", drawTicks: false },
              ticks: {
                color: "#9298aa",
                maxTicksLimit: 5,
                padding: 10,
                callback: value => field === "accuracy" ? `${value}%` : value,
                font: { size: 11 }
              }
            }
          }
        }
      });
    }

    function renderHistory(records) {
      if (records.length === 0) {
        historyContent.hidden = true;
        historyStatus.textContent = "No saved rounds for this trick yet. Complete a round to see your progress.";
        return;
      }

      const selectedLength = historyDuration.value;
      const selectedFirstDigits = historyFirstDigits.value;
      const selectedSecondDigits = historySecondDigits.value;
      const lengthLabel = selectedLength === "120"
        ? "2-minute Sprint"
        : selectedLength === "600"
          ? "10-minute Endurance"
          : "all round lengths";
      const operator = mode.value === "addition" ? "+" : "×";
      const digitsLabel = selectedFirstDigits === "all" && selectedSecondDigits === "all"
        ? "all digit lengths"
        : `${selectedFirstDigits === "all" ? "any" : selectedFirstDigits}-digit ${operator} ` +
          `${selectedSecondDigits === "all" ? "any" : selectedSecondDigits}-digit`;
      historyStatus.textContent =
        `Showing ${records.length === 100 ? "your latest 100" : records.length} ${lengthLabel}` +
        `${usesOperandDigits(mode.value) ? ` · ${digitsLabel}` : ""}, oldest to newest.`;
      historyContent.hidden = false;
      if (typeof Chart === "undefined") {
        historyCharts.hidden = true;
        historyStatus.textContent += " Charts are unavailable; the round-by-round table is still shown.";
      } else {
        historyCharts.hidden = false;
        const chartFilterLabel = usesOperandDigits(mode.value)
          ? `${lengthLabel} · ${digitsLabel}`
          : lengthLabel;
        accuracyChartTitle.textContent = `Accuracy · ${chartFilterLabel}`;
        correctChartTitle.textContent = `Correct answers · ${chartFilterLabel}`;
        document.querySelector("#accuracy-chart").setAttribute(
          "aria-label", `Accuracy over ${records.length} rounds`
        );
        document.querySelector("#correct-chart").setAttribute(
          "aria-label", `Correct answers over ${records.length} rounds`
        );
        accuracyChart = drawChart(
          document.querySelector("#accuracy-chart"), accuracyChart, records, "accuracy", 100, false
        );
        const correctMaximum = Math.max(1, Math.ceil(Math.max(...records.map(row => row.correct)) / 4) * 4);
        correctChart = drawChart(
          document.querySelector("#correct-chart"), correctChart, records, "correct", correctMaximum, true
        );
      }

      const rows = document.querySelector("#history-rows");
      rows.replaceChildren();
      for (const record of [...records].reverse()) {
        const row = document.createElement("tr");
        const date = new Date(record.completed_at).toLocaleString();
        const values = [
          date,
          Number(record.duration_limit_seconds) === 600 ? "10 min" : "2 min",
          usesOperandDigits(mode.value)
            && record.first_operand_digits !== null
            && record.second_operand_digits !== null
            ? `${record.first_operand_digits}-digit ${operator} ${record.second_operand_digits}-digit`
            : "—",
          record.attempted,
          record.correct,
          `${Number(record.accuracy).toFixed(0)}%`,
          `${Number(record.average_answer_seconds).toFixed(1)}s`,
          record.best_streak
        ];
        for (const value of values) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        }
        rows.append(row);
      }
    }

    async function loadHistory() {
      const requestId = ++historyRequestId;
      const selectedMode = mode.value;
      const selectedLength = historyDuration.value;
      const selectedFirstDigits = usesOperandDigits(selectedMode) ? historyFirstDigits.value : "all";
      const selectedSecondDigits = usesOperandDigits(selectedMode) ? historySecondDigits.value : "all";
      historyContent.hidden = true;
      historyStatus.textContent = "Loading saved rounds...";
      historyFirstFilterWrap.hidden = !usesOperandDigits(selectedMode);
      historySecondFilterWrap.hidden = !usesOperandDigits(selectedMode);
      try {
        const response = await fetch(
          `/history?mode=${encodeURIComponent(selectedMode)}&duration=${encodeURIComponent(selectedLength)}` +
          `&first_digits=${encodeURIComponent(selectedFirstDigits)}` +
          `&second_digits=${encodeURIComponent(selectedSecondDigits)}`
        );
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Could not load history.");
        if (
          requestId !== historyRequestId
          || selectedMode !== mode.value
          || selectedLength !== historyDuration.value
          || selectedFirstDigits !== (usesOperandDigits(selectedMode) ? historyFirstDigits.value : "all")
          || selectedSecondDigits !== (usesOperandDigits(selectedMode) ? historySecondDigits.value : "all")
        ) return;
        renderHistory(result.rounds);
      } catch (error) {
        if (requestId !== historyRequestId) return;
        console.error("Could not load round history:", error);
        historyStatus.textContent =
          `Could not load history: ${error instanceof Error ? error.message : "Unknown error."}`;
      }
    }

    historyToggle.addEventListener("click", () => {
      historyPanel.hidden = !historyPanel.hidden;
      historyToggle.setAttribute("aria-expanded", String(!historyPanel.hidden));
      historyToggle.textContent = historyPanel.hidden ? "View history" : "Hide history";
      if (!historyPanel.hidden) loadHistory();
    });

    historyDuration.addEventListener("change", () => {
      if (!historyPanel.hidden) loadHistory();
    });

    historyFirstDigits.addEventListener("change", () => {
      if (!historyPanel.hidden) loadHistory();
    });

    historySecondDigits.addEventListener("change", () => {
      if (!historyPanel.hidden) loadHistory();
    });

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!running) return;
      if (performance.now() >= deadline) {
        finishRound();
        return;
      }
      if (answer.value === "") {
        feedback.textContent = "Enter a whole number to submit.";
        feedback.style.color = "#c04b38";
        answer.focus();
        return;
      }

      attempted += 1;
      answerTimeTotal += performance.now() - questionStarted;
      const expectedAnswer = mode.value === "addition"
        ? first + second
        : first * second;
      if (Number(answer.value) === expectedAnswer) {
        correct += 1;
        streak += 1;
        bestStreak = Math.max(bestStreak, streak);
        feedback.textContent = "Correct!";
        feedback.style.color = "#21845b";
      } else {
        incorrect += 1;
        streak = 0;
        feedback.textContent = `Not quite — the answer was ${expectedAnswer.toLocaleString()}.`;
        feedback.style.color = "#c04b38";
      }
      updateStats();
      answer.value = "";
      if (performance.now() >= deadline) {
        finishRound();
        return;
      }
      nextQuestion();
      answer.focus();
    });

    start.addEventListener("click", () => {
      if (running) finishRound();
      else startRound();
    });

    mode.addEventListener("change", () => {
      const instructions = {
        addition: {
          subtitle: "Practice addition with independently chosen addend lengths.",
          method: "Choose the digit range for each number independently—for example, a two-digit number plus a one-digit number."
        },
        "single-digit-multiplication": {
          subtitle: "Practice multiplication with independently chosen factor lengths.",
          method: "Choose the digit range for each factor independently—for example, a two-digit number times a one-digit number."
        },
        squares: {
          subtitle: "Square two-digit numbers ending in 5.",
          method: "Multiply the digits before 5 by the next number, then append 25. Example: 35² → 3 × 4, then 25 = 1225."
        },
        complements: {
          subtitle: "Multiply pairs with the same tens digit and ones digits that sum to 10.",
          method: "Multiply the shared tens digit by the next number, then append the product of the ones digits (use two digits). Example: 43 × 47 → 4 × 5, then 3 × 7 = 2021."
        },
        "eleven-two": {
          subtitle: "Multiply a two-digit number by 11.",
          method: "Add the two digits and place their sum between them; carry if the sum is 10 or more. Example: 35 × 11 → 3 | 3+5 | 5 = 385."
        },
        "eleven-three": {
          subtitle: "Multiply a three-digit number by 11.",
          method: "Keep the outside digits; add each neighboring pair for the middle digits, carrying as needed. Example: 314 × 11 → 3 | 3+1 | 1+4 | 4 = 3454."
        }
      };
      subtitle.textContent = instructions[mode.value].subtitle;
      method.textContent = instructions[mode.value].method;
      const hasOperandDigits = usesOperandDigits(mode.value);
      const operator = mode.value === "addition" ? "+" : "×";
      const firstLabel = mode.value === "addition" ? "FIRST ADDEND" : "FIRST FACTOR";
      const secondLabel = mode.value === "addition" ? "SECOND ADDEND" : "SECOND FACTOR";
      operandDigitsPicker.hidden = !hasOperandDigits;
      firstOperandLabel.textContent = firstLabel;
      secondOperandLabel.textContent = secondLabel;
      operandOperator.textContent = operator;
      historyFirstLabel.textContent = firstLabel;
      historySecondLabel.textContent = secondLabel;
      question.textContent = "Ready?";
      if (!historyPanel.hidden) loadHistory();
    });

    roundLength.addEventListener("change", () => {
      roundSeconds = Number(roundLength.value);
      roundLabel.textContent = roundSeconds === 120
        ? "2-minute sprint"
        : "10-minute endurance";
      timer.textContent = roundSeconds === 120 ? "02:00" : "10:00";
    });
  </script>
</body>
</html>
"""


class QuizRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/history":
            query = parse_qs(request.query)
            modes = query.get("mode", [])
            durations = query.get("duration", ["all"])
            first_digits = query.get("first_digits", ["all"])
            second_digits = query.get("second_digits", ["all"])
            if len(modes) != 1:
                self._send_json(400, {"error": "Select exactly one quiz method."})
                return
            if len(durations) != 1 or durations[0] not in ("all", "120", "600"):
                self._send_json(400, {"error": "Select all round lengths, 120 seconds, or 600 seconds."})
                return
            valid_digit_values = {"all", *(str(value) for value in OPERAND_DIGIT_LENGTHS)}
            if (
                len(first_digits) != 1
                or first_digits[0] not in valid_digit_values
                or len(second_digits) != 1
                or second_digits[0] not in valid_digit_values
            ):
                self._send_json(400, {"error": "Select all digit lengths or a digit length from 1 to 6 for each operand."})
                return
            try:
                duration = None if durations[0] == "all" else int(durations[0])
                rounds = load_history(
                    modes[0],
                    duration_limit_seconds=duration,
                    first_operand_digits=(
                        None if first_digits[0] == "all" else int(first_digits[0])
                    ),
                    second_operand_digits=(
                        None if second_digits[0] == "all" else int(second_digits[0])
                    ),
                )
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            except sqlite3.Error:
                logging.exception("Failed to load round history from SQLite")
                self._send_json(500, {"error": "The SQLite database could not load history."})
                return
            self._send_json(200, {"rounds": rounds})
            return

        if request.path != "/":
            self.send_error(404)
            return

        page = PAGE.replace(
            "__DEFAULT_ROUND_SECONDS__", str(DEFAULT_ROUND_SECONDS)
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:
        if self.path != "/save-round":
            self._send_json(404, {"error": "Not found."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length header."})
            return
        if content_length < 0 or content_length > 16_384:
            self._send_json(413, {"error": "Round data is too large."})
            return

        try:
            data = json.loads(self.rfile.read(content_length))
            save_round(data)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._send_json(400, {"error": str(error)})
            return
        except sqlite3.Error:
            logging.exception("Failed to save round stats to SQLite")
            self._send_json(500, {"error": "The SQLite database could not save this round."})
            return

        self._send_json(201, {"status": "saved"})


def main() -> None:
    initialize_database()
    server = HTTPServer(("127.0.0.1", 0), QuizRequestHandler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Opening Mental Math Sprint at {url}")
    print("Keep this window open while you play. Press Ctrl+C to quit.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMental Math Sprint stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
