/**
 * BLACKTOP — the driver-facing app.
 *
 * The whole flow: screenshot the offer card, share it here (or drop it in),
 * Claude reads the fields, and the local engine rules on it against a
 * reservation rate learned from this driver's own history.
 *
 * Two things the UI must never do, because they are the difference between a
 * tool and a liability:
 *   - act on the driver's behalf. It advises; the human taps (I3/I4).
 *   - show a confident number it is not confident about. Below the extraction
 *     gate it says so and gets out of the way.
 */

import { DriverSession } from "./session";
import { extractOffer } from "../vision/client";
import type { Offer, Verdict } from "../domain/models";
import { totalMinutes } from "../domain/models";
import type { RateDecision } from "../domain/reservation";

const app = document.querySelector<HTMLElement>("#app");
if (!app) throw new Error("missing #app root");

const session = new DriverSession({
  mpg: 28, fuelPricePerGallon: 3.4, kwhPerMile: null,
  electricityPricePerKwh: null, maintenancePerMile: 0.06, depreciationPerMile: 0.1,
});

interface Screen {
  busy: boolean;
  error: string | null;
  verdict: Verdict | null;
  rate: RateDecision | null;
  offer: Offer | null;
  latencyMs: number;
  logged: number;
}

const screen: Screen = {
  busy: false, error: null, verdict: null, rate: null,
  offer: null, latencyMs: 0, logged: 0,
};

const money = (n: number) => `$${n.toFixed(2)}`;
const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);

const VERDICT_WORD: Record<Verdict["color"], string> = {
  green: "TAKE IT",
  amber: "CLOSE",
  red: "SKIP",
  manual_fallback: "Couldn't read it",
};

/** Audio is the primary channel; the driver's eyes belong on the road. */
function speak(text: string): void {
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.1;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(u);
}

function renderVerdict(): string {
  const { verdict: v, offer, rate } = screen;
  if (!v) return "";
  const tb = v.timeBreakdown;
  const compare = v.color === "manual_fallback"
    ? `<p class="verdict-sub">Read it yourself — this one's your call.</p>`
    : `<p class="verdict-compare">${Math.round(v.projectedNetHourly)}
         <span class="against">vs ${Math.round(v.reservationRateHourly)}/hr</span></p>
       <p class="verdict-sub">${esc(offer?.merchantName ?? "")} ·
         ${money(v.expectedPayout)} over ${tb ? Math.round(totalMinutes(tb)) : "?"} min</p>`;

  // The deadhead and the merchant wait are the whole reason this product
  // exists, so they are the second thing shown (PRD §9.6).
  const costs = tb ? `
    <div class="costs">
      <div class="cost"><b>${Math.round(tb.merchantWaitMin)} min</b><span>merchant wait</span></div>
      <div class="cost"><b>${Math.round(tb.returnToDensityMin)} min</b><span>deadhead back</span></div>
      <div class="cost" data-hidden="true"><b>${money(v.expectedHiddenTip)}</b><span>hidden tip est.</span></div>
      <div class="cost"><b>${Math.round(tb.driveToMerchantMin + tb.driveToCustomerMin)} min</b><span>driving</span></div>
    </div>` : "";

  const notes = rate?.notes.length
    ? `<ul class="notes">${rate.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`
    : "";

  return `
    <section class="card verdict" data-color="${v.color}">
      <p class="verdict-word">${VERDICT_WORD[v.color]}</p>
      ${compare}
      ${costs}
    </section>
    <section class="card">
      <h2>What you did</h2>
      <div class="actions">
        <button class="go" data-act="accepted">I accepted</button>
        <button data-act="declined">I declined</button>
      </div>
      <p class="muted" style="margin:.7rem 0 0">
        Recording this is what teaches the tip model — it is the only label that costs nothing.
      </p>
      ${notes}
    </section>`;
}

function render(): void {
  const online = navigator.onLine;
  const net = session.mileage;
  const hourly = session.onlineMinutes > 0
    ? net.trueNetHourly(session.onlineMinutes) : 0;

  app!.innerHTML = `
    <div class="topbar">
      <span class="brand">BLACKTOP</span>
      <span class="muted">
        <span class="online-dot" data-on="${online}"></span>
        ${online ? "reader online" : "offline — reader unavailable"}
      </span>
    </div>

    <section class="card">
      <h2>Session</h2>
      <div class="stats">
        <div class="stat"><b>${session.startedAt ? Math.round(session.onlineMinutes) : 0}m</b><span>online</span></div>
        <div class="stat"><b>${money(hourly)}</b><span>true net/hr</span></div>
        <div class="stat"><b>${Math.round(session.acceptanceRate * 100)}%</b><span>accept rate</span></div>
      </div>
      <div class="actions" style="margin-top:.7rem">
        ${session.startedAt
          ? `<button class="stop wide" data-act="stop">Go offline</button>`
          : `<button class="go wide" data-act="start">Go online</button>`}
      </div>
      <p class="muted" style="margin:.7rem 0 0">
        Shielded by mileage: <b>${money(net.shieldedIncome)}</b> of ${money(net.grossEarnings)} gross is tax-free.
      </p>
    </section>

    ${screen.busy
      ? `<section class="card"><p class="spinner">Reading the card…</p></section>`
      : ""}
    ${screen.error ? `<section class="card"><p class="error">${esc(screen.error)}</p></section>` : ""}
    ${renderVerdict()}

    <section class="card">
      <h2>Score an offer</h2>
      <div class="dropzone" id="drop">
        Share a screenshot to BLACKTOP, or drop / paste one here.
      </div>
      <div class="actions" style="margin-top:.7rem">
        <button class="primary wide" data-act="pick">Choose screenshot</button>
      </div>
      <input type="file" accept="image/*" id="file" class="hidden" />
      <p class="muted" style="margin:.7rem 0 0">
        ${screen.logged} offers logged${screen.latencyMs ? ` · last read ${screen.latencyMs} ms` : ""}
      </p>
    </section>

    <p class="muted" style="text-align:center">
      Advisory only. BLACKTOP never taps anything — every accept and decline is yours.
    </p>`;

  wire();
}

async function handleImage(blob: Blob): Promise<void> {
  screen.busy = true;
  screen.error = null;
  screen.verdict = null;
  render();

  const now = new Date();
  const id = `o-${now.getTime()}`;
  const result = await extractOffer(blob, id, now, session.tips.capDetector.capValue);
  screen.busy = false;
  screen.latencyMs = result.latencyMs;

  if (!result.offer) {
    screen.error = result.error ?? "Couldn't read that one.";
    speak("Couldn't read it. Your call.");
    render();
    return;
  }

  const { verdict, rate } = await session.score(result.offer, result.confidence, now);
  screen.offer = result.offer;
  screen.verdict = verdict;
  screen.rate = rate;
  screen.logged += 1;
  speak(verdict.ttsText);
  render();
}

function wire(): void {
  const root = app!;
  root.querySelector<HTMLInputElement>("#file")?.addEventListener("change", (e) => {
    const f = (e.target as HTMLInputElement).files?.[0];
    if (f) void handleImage(f);
  });

  root.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = btn.dataset["act"];
      if (act === "pick") root.querySelector<HTMLInputElement>("#file")?.click();
      else if (act === "start") {
        session.startedAt = new Date();
        render();
      } else if (act === "stop") {
        session.startedAt = null;
        render();
      } else if (act === "accepted" && screen.offer && screen.verdict) {
        const tb = screen.verdict.timeBreakdown;
        void session.recordAccept(
          screen.offer, tb ? totalMinutes(tb) : 25, new Date()).then(render);
      } else if (act === "declined" && screen.offer) {
        void session.recordDecline(screen.offer.offerId).then(() => {
          screen.verdict = null;
          screen.offer = null;
          render();
        });
      }
    });
  });

  const drop = root.querySelector<HTMLElement>("#drop");
  if (drop) {
    drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      drop.classList.add("hot");
    });
    drop.addEventListener("dragleave", () => drop.classList.remove("hot"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("hot");
      const f = e.dataTransfer?.files?.[0];
      if (f) void handleImage(f);
    });
  }
}

/** Pick up a screenshot handed over by the share-target service worker. */
async function collectSharedScreenshot(): Promise<void> {
  if (!new URLSearchParams(location.search).has("shared")) return;
  history.replaceState(null, "", "/");
  try {
    const res = await fetch("/__shared-screenshot");
    if (res.status === 200) await handleImage(await res.blob());
  } catch {
    screen.error = "That share didn't come through — try dropping the file instead.";
    render();
  }
}

window.addEventListener("paste", (e) => {
  const item = [...(e.clipboardData?.items ?? [])].find((i) => i.type.startsWith("image/"));
  const file = item?.getAsFile();
  if (file) void handleImage(file);
});

window.addEventListener("online", render);
window.addEventListener("offline", render);

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/sw.js");
  });
}

void session.hydrate().then((n) => {
  screen.logged = n;
  render();
  void collectSharedScreenshot();
});

render();
