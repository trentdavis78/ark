/**
 * F7 — Mileage & Tax Engine.
 *
 * Mileage is here because it is the largest financial lever a driver has, not
 * because this competes with bookkeeping software. Two things most trackers
 * get wrong and this does not:
 *
 *   1. All online miles are business miles — including deadhead and
 *      repositioning. Logging only delivery legs forfeits 30–40% of the
 *      deduction.
 *   2. 2026 splits mid-year. A single annual rate applied to 2026 is wrong.
 *
 * Estimates for planning. Not tax advice.
 */

export const RATE_2026_H1 = 0.725; // through June 30, 2026
export const RATE_2026_H2 = 0.76; //  from July 1, 2026

/** Federal and NJ estimated-payment due dates for tax year 2026. */
export const QUARTERLY_DUE_DATES_2026 = [
  new Date(2026, 3, 15), new Date(2026, 5, 15),
  new Date(2026, 8, 15), new Date(2027, 0, 15),
] as const;

/**
 * The rate applicable to miles driven on `d`. Only 2026 is modelled, and any
 * other year throws rather than silently applying a wrong rate to someone's
 * tax return.
 */
export function mileageRate(d: Date): number {
  if (d.getFullYear() !== 2026) {
    throw new RangeError(`rate table only covers 2026, got ${d.getFullYear()}`);
  }
  return d.getTime() < new Date(2026, 6, 1).getTime() ? RATE_2026_H1 : RATE_2026_H2;
}

export interface VehicleCostParams {
  mpg: number | null;
  fuelPricePerGallon: number | null;
  kwhPerMile: number | null;
  electricityPricePerKwh: number | null;
  maintenancePerMile: number;
  depreciationPerMile: number;
}

export const DEFAULT_VEHICLE: VehicleCostParams = {
  mpg: null, fuelPricePerGallon: null, kwhPerMile: null,
  electricityPricePerKwh: null, maintenancePerMile: 0.06, depreciationPerMile: 0.1,
};

export function energyCostPerMile(v: VehicleCostParams): number {
  if (v.mpg && v.fuelPricePerGallon) return v.fuelPricePerGallon / v.mpg;
  if (v.kwhPerMile && v.electricityPricePerKwh) {
    return v.kwhPerMile * v.electricityPricePerKwh;
  }
  return 0;
}

export const costPerMile = (v: VehicleCostParams): number =>
  energyCostPerMile(v) + v.maintenancePerMile + v.depreciationPerMile;

export interface MileageEntry {
  date: Date;
  startTime: Date;
  endTime: Date;
  startLoc: [number, number];
  endLoc: [number, number];
  miles: number;
  businessPurpose: string;
  routePolyline: string;
}

const dayKey = (d: Date): string => d.toISOString().slice(0, 10);

/**
 * Session-boundary mileage capture (online → offline), accumulating an
 * audit-defensible entry per session.
 */
export class SessionMileageLog {
  readonly entries: MileageEntry[] = [];
  grossEarnings = 0;

  constructor(
    public vehicle: VehicleCostParams = DEFAULT_VEHICLE,
    /** SE + marginal federal + NJ, for the live reserve display. */
    public marginalTaxRate = 0.3,
  ) {}

  logSession(e: Omit<MileageEntry, "date" | "businessPurpose" | "routePolyline"> & {
    businessPurpose?: string; routePolyline?: string; gross: number;
  }): MileageEntry {
    if (e.endTime.getTime() <= e.startTime.getTime()) {
      throw new RangeError("session end must be after start");
    }
    if (e.miles < 0 || e.gross < 0) throw new RangeError("negative miles or gross");
    const entry: MileageEntry = {
      date: e.startTime,
      startTime: e.startTime,
      endTime: e.endTime,
      startLoc: e.startLoc,
      endLoc: e.endLoc,
      miles: e.miles,
      businessPurpose: e.businessPurpose ?? "delivery driving — all online miles",
      routePolyline: e.routePolyline ?? "",
    };
    this.entries.push(entry);
    this.grossEarnings += e.gross;
    return entry;
  }

  get totalMiles(): number {
    return this.entries.reduce((s, e) => s + e.miles, 0);
  }

  milesByDate(): Map<string, { date: Date; miles: number }> {
    const out = new Map<string, { date: Date; miles: number }>();
    for (const e of this.entries) {
      const k = dayKey(e.date);
      const cur = out.get(k);
      if (cur) cur.miles += e.miles;
      else out.set(k, { date: e.date, miles: e.miles });
    }
    return out;
  }

  /** Deduction with the correct per-date rate applied to each day's miles. */
  get deduction(): number {
    let total = 0;
    for (const { date, miles } of this.milesByDate().values()) {
      total += miles * mileageRate(date);
    }
    return total;
  }

  /**
   * How much of today's gross is tax-free because of the mileage deduction.
   * This is the number that reframes the business for a driver, and no other
   * app surfaces it.
   */
  get shieldedIncome(): number {
    return Math.min(this.grossEarnings, this.deduction);
  }

  get vehicleCost(): number {
    return this.totalMiles * costPerMile(this.vehicle);
  }

  get taxReserve(): number {
    const taxable = Math.max(0, this.grossEarnings - this.deduction);
    return taxable * this.marginalTaxRate;
  }

  /** Gross minus true vehicle cost minus tax reserve — the PRD §4 north star. */
  get trueNet(): number {
    return this.grossEarnings - this.vehicleCost - this.taxReserve;
  }

  trueNetHourly(onlineMinutes: number): number {
    return onlineMinutes <= 0 ? 0 : (this.trueNet / onlineMinutes) * 60;
  }
}

export interface TaxEstimate {
  netProfit: number;
  selfEmploymentTax: number;
  federalIncomeTax: number;
  stateIncomeTax: number;
}

export const totalTax = (t: TaxEstimate): number =>
  t.selfEmploymentTax + t.federalIncomeTax + t.stateIncomeTax;

export const quarterlyPayment = (t: TaxEstimate): number => totalTax(t) / 4;

/**
 * SE tax: 15.3% on 92.35% of net profit, which is the part trackers that just
 * multiply by 0.153 get wrong.
 */
export function selfEmploymentTax(netProfit: number): number {
  if (netProfit <= 0) return 0;
  return netProfit * 0.9235 * 0.153;
}

function bracketTax(taxable: number, brackets: ReadonlyArray<[number, number]>): number {
  let tax = 0;
  let last = 0;
  for (const [cap, rate] of brackets) {
    if (taxable <= last) break;
    tax += (Math.min(taxable, cap) - last) * rate;
    last = cap;
  }
  return tax;
}

/** Single-filer federal brackets, 2026 (approximate, indexed). */
const FEDERAL_BRACKETS: ReadonlyArray<[number, number]> = [
  [12_400, 0.10], [50_400, 0.12], [105_700, 0.22],
  [201_775, 0.24], [256_225, 0.32], [640_600, 0.35],
  [Number.POSITIVE_INFINITY, 0.37],
];

/** NJ gross income tax, single filer. */
const NJ_BRACKETS: ReadonlyArray<[number, number]> = [
  [20_000, 0.014], [35_000, 0.0175], [40_000, 0.035],
  [75_000, 0.05525], [500_000, 0.0637], [1_000_000, 0.0897],
  [Number.POSITIVE_INFINITY, 0.1075],
];

export function estimateTaxes(
  grossIncome: number,
  mileageDeduction: number,
  otherExpenses = 0,
  standardDeduction = 16_100,
): TaxEstimate {
  const netProfit = Math.max(0, grossIncome - mileageDeduction - otherExpenses);
  const se = selfEmploymentTax(netProfit);
  // Half of SE tax is an above-the-line deduction.
  const agi = Math.max(0, netProfit - se / 2);
  const federalTaxable = Math.max(0, agi - standardDeduction);
  return {
    netProfit,
    selfEmploymentTax: se,
    federalIncomeTax: bracketTax(federalTaxable, FEDERAL_BRACKETS),
    // NJ has no standard deduction; it taxes gross income.
    stateIncomeTax: bracketTax(netProfit, NJ_BRACKETS),
  };
}
