/**
 * The 14-slot fleet tuple trap.
 *
 * Every fleet entrypoint on VeydriftGame (launchFleetMission, and the MissionShips struct in
 * general) takes a fixed `(uint32 x 14)` tuple -- but the `Ship` enum has 16 members. Two of them
 * cannot fly and are omitted from the tuple:
 *
 *   Ship.SolarSatellite = 9    Ship.Crawler = 15
 *
 * Source: VeydriftFleetFuel.sol:73-87 (cited in RESEARCH-ADDENDUM.md §3), and confirmed directly
 * against the pinned ABI's `MissionShips` tuple component names at commit
 * 701bed3578cff4d134657c714c599dbdb55a4b6a (packages/contracts/src/libraries/VeydriftTypes.sol:43-60
 * for the Ship enum order).
 *
 * Because SolarSatellite sits at id 9 -- squarely in the middle of the enum -- every flyable ship
 * id greater than 9 is shifted down by exactly one tuple slot. A Destroyer (Ship id 10) lands at
 * tuple index 9, NOT 10. Indexing the tuple with a raw Ship id sends Destroyers where Solar
 * Satellites were meant (or worse, silently truncates/misaligns the whole rest of the fleet).
 * This is a silent-corruption bug, not a crash, which is why it gets a dedicated function and a
 * dedicated test rather than being hand-rolled at each call site.
 */

/** Ship ids, matching `enum Ship` in VeydriftTypes.sol exactly (0-15). */
export enum ShipId {
  SmallCargo = 0,
  LightFighter = 1,
  Recycler = 2,
  ColonyShip = 3,
  LargeCargo = 4,
  HeavyFighter = 5,
  Cruiser = 6,
  Battleship = 7,
  Bomber = 8,
  SolarSatellite = 9, // NOT FLYABLE -- absent from the fleet tuple
  Destroyer = 10,
  Deathstar = 11,
  Battlecruiser = 12,
  Reaper = 13,
  Pathfinder = 14,
  Crawler = 15, // NOT FLYABLE -- absent from the fleet tuple
}

export const NON_FLYABLE_SHIP_IDS: ReadonlySet<ShipId> = new Set([
  ShipId.SolarSatellite,
  ShipId.Crawler,
]);

export const FLEET_TUPLE_LENGTH = 14;

/** Tuple index -> Ship id, in the exact order of the `MissionShips` struct components on the
 *  pinned ABI (smallCargo, lightFighter, recycler, colonyShip, largeCargo, heavyFighter, cruiser,
 *  battleship, bomber, destroyer, deathstar, battlecruiser, reaper, pathfinder). */
export const FLEET_TUPLE_ORDER: readonly ShipId[] = [
  ShipId.SmallCargo, // 0
  ShipId.LightFighter, // 1
  ShipId.Recycler, // 2
  ShipId.ColonyShip, // 3
  ShipId.LargeCargo, // 4
  ShipId.HeavyFighter, // 5
  ShipId.Cruiser, // 6
  ShipId.Battleship, // 7
  ShipId.Bomber, // 8
  ShipId.Destroyer, // 9  <- NOT 10. This is the trap.
  ShipId.Deathstar, // 10
  ShipId.Battlecruiser, // 11
  ShipId.Reaper, // 12
  ShipId.Pathfinder, // 13
];

/** Ship id -> tuple index, derived from FLEET_TUPLE_ORDER (not hand-duplicated). */
export const SHIP_ID_TO_TUPLE_INDEX: ReadonlyMap<ShipId, number> = new Map(
  FLEET_TUPLE_ORDER.map((shipId, index) => [shipId, index]),
);

export type FleetTuple = readonly [
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
  bigint,
];

/**
 * Convert a sparse map of { shipId -> count } into the 14-slot tuple the contract expects, in
 * the correct (shifted) order.
 *
 * Throws if `counts` contains a key for a non-flyable ship (SolarSatellite or Crawler) at all --
 * even a zero count is rejected, because a caller writing `{ [ShipId.SolarSatellite]: 0 }` is a
 * caller who thinks Solar Satellites belong in a fleet tuple, and that assumption is exactly what
 * this function exists to catch before it reaches an RPC call.
 */
export function shipCountsToFleetTuple(
  counts: Partial<Record<ShipId, number | bigint>> | Map<ShipId, number | bigint>,
): FleetTuple {
  const entries: [ShipId, number | bigint][] =
    counts instanceof Map ? [...counts.entries()] : (Object.entries(counts) as unknown as [string, number | bigint][]).map(([k, v]) => [Number(k) as ShipId, v]);

  const tuple: bigint[] = new Array(FLEET_TUPLE_LENGTH).fill(0n);

  for (const [shipId, rawCount] of entries) {
    if (NON_FLYABLE_SHIP_IDS.has(shipId)) {
      const name = ShipId[shipId] ?? `id ${shipId}`;
      throw new Error(
        `shipCountsToFleetTuple: Ship.${name} (id ${shipId}) cannot fly and has no slot in the ` +
          `14-slot fleet tuple. It must never appear in fleet-mission input.`,
      );
    }
    const index = SHIP_ID_TO_TUPLE_INDEX.get(shipId);
    if (index === undefined) {
      throw new Error(`shipCountsToFleetTuple: unknown Ship id ${shipId}`);
    }
    const count = typeof rawCount === "bigint" ? rawCount : BigInt(rawCount);
    if (count < 0n) {
      throw new Error(`shipCountsToFleetTuple: negative count for Ship.${ShipId[shipId]}`);
    }
    tuple[index] = count;
  }

  return tuple as unknown as FleetTuple;
}

/** Inverse of shipCountsToFleetTuple, for decoding/printing an existing tuple back to ship ids.
 *  Useful for `walletctl send`'s decoded-args printout. */
export function fleetTupleToShipCounts(tuple: readonly (number | bigint)[]): Partial<Record<ShipId, bigint>> {
  if (tuple.length !== FLEET_TUPLE_LENGTH) {
    throw new Error(`fleetTupleToShipCounts: expected ${FLEET_TUPLE_LENGTH} slots, got ${tuple.length}`);
  }
  const out: Partial<Record<ShipId, bigint>> = {};
  for (let i = 0; i < FLEET_TUPLE_LENGTH; i++) {
    const raw = tuple[i] ?? 0;
    const count = typeof raw === "bigint" ? raw : BigInt(raw);
    if (count !== 0n) {
      const shipId = FLEET_TUPLE_ORDER[i] as ShipId;
      out[shipId] = count;
    }
  }
  return out;
}
