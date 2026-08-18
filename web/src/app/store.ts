/**
 * Local persistence. Everything the driver learns stays on their device.
 *
 * IndexedDB rather than localStorage: the offer log is the training set for
 * the tip model and the counterfactual replay, so it grows without bound and
 * must survive reloads. Declines are stored too — a declined offer is an
 * observation about that hour and place, not an absence (PRD §8).
 */

const DB_NAME = "blacktop";
const DB_VERSION = 1;

export interface StoredOffer {
  id: string;
  seenAt: number;
  displayedPayout: number;
  merchantName: string;
  merchantId: string | null;
  statedDistanceMi: number | null;
  statedMinutes: number | null;
  hitDisplayCap: boolean;
  projectedMinutes: number;
  projectedPayout: number;
  reservationHourly: number;
  verdict: string;
  extractionConfidence: number;
  /** What the driver actually did, recorded after the fact. */
  action: "accepted" | "declined" | null;
  /** The free label: actual payout once the delivery completes. */
  actualPayout: number | null;
  actualMinutes: number | null;
}

export interface StoredSession {
  id: string;
  startedAt: number;
  endedAt: number | null;
  miles: number;
  gross: number;
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("offers")) {
        const offers = db.createObjectStore("offers", { keyPath: "id" });
        offers.createIndex("seenAt", "seenAt");
      }
      if (!db.objectStoreNames.contains("sessions")) {
        db.createObjectStore("sessions", { keyPath: "id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexedDB open failed"));
  });
}

function tx<T>(
  store: string,
  mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return open().then((db) => new Promise<T>((resolve, reject) => {
    const t = db.transaction(store, mode);
    const req = fn(t.objectStore(store));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error(`${store} request failed`));
    t.oncomplete = () => db.close();
  }));
}

export const putOffer = (o: StoredOffer): Promise<IDBValidKey> =>
  tx("offers", "readwrite", (s) => s.put(o));

export const allOffers = (): Promise<StoredOffer[]> =>
  tx<StoredOffer[]>("offers", "readonly", (s) => s.getAll());

export const putSession = (s: StoredSession): Promise<IDBValidKey> =>
  tx("sessions", "readwrite", (st) => st.put(s));

export const allSessions = (): Promise<StoredSession[]> =>
  tx<StoredSession[]>("sessions", "readonly", (s) => s.getAll());

export async function updateOffer(
  id: string,
  patch: Partial<StoredOffer>,
): Promise<void> {
  const existing = await tx<StoredOffer | undefined>(
    "offers", "readonly", (s) => s.get(id));
  if (!existing) return;
  await putOffer({ ...existing, ...patch });
}
