import { describe, expect, it } from "vitest";
import {
  FLEET_TUPLE_LENGTH,
  fleetTupleToShipCounts,
  SHIP_ID_TO_TUPLE_INDEX,
  ShipId,
  shipCountsToFleetTuple,
} from "../src/fleet.js";

describe("shipCountsToFleetTuple -- the 14-slot fleet tuple trap", () => {
  it("places a Destroyer at tuple index 9, NOT 10 (the trap this function exists for)", () => {
    const tuple = shipCountsToFleetTuple({ [ShipId.Destroyer]: 5 });
    expect(tuple.length).toBe(FLEET_TUPLE_LENGTH);
    expect(tuple[9]).toBe(5n);
    expect(tuple[10]).toBe(0n); // index 10 is Deathstar -- must stay zero
    expect(SHIP_ID_TO_TUPLE_INDEX.get(ShipId.Destroyer)).toBe(9);
  });

  it("throws on SolarSatellite (Ship id 9) -- cannot fly, no tuple slot", () => {
    expect(() => shipCountsToFleetTuple({ [ShipId.SolarSatellite]: 1 })).toThrow(/SolarSatellite/);
  });

  it("throws on SolarSatellite even with an explicit zero count", () => {
    expect(() => shipCountsToFleetTuple({ [ShipId.SolarSatellite]: 0 })).toThrow(/SolarSatellite/);
  });

  it("throws on Crawler (Ship id 15) -- cannot fly, no tuple slot", () => {
    expect(() => shipCountsToFleetTuple({ [ShipId.Crawler]: 3 })).toThrow(/Crawler/);
  });

  it("places every flyable ship at its correctly shifted index", () => {
    const counts: Partial<Record<ShipId, number>> = {};
    for (let id = 0; id <= 15; id++) {
      if (id === ShipId.SolarSatellite || id === ShipId.Crawler) continue;
      counts[id as ShipId] = id + 100;
    }
    const tuple = shipCountsToFleetTuple(counts);
    // ids 0-8 map 1:1 (below the SolarSatellite gap)
    for (let id = 0; id <= 8; id++) {
      expect(tuple[id]).toBe(BigInt(id + 100));
    }
    // ids 10-14 shift down by exactly one slot (above the SolarSatellite gap)
    for (let id = 10; id <= 14; id++) {
      expect(tuple[id - 1]).toBe(BigInt(id + 100));
    }
  });

  it("defaults every omitted slot to zero", () => {
    const tuple = shipCountsToFleetTuple({});
    expect(tuple.every((v) => v === 0n)).toBe(true);
    expect(tuple.length).toBe(14);
  });

  it("accepts a Map input as well as a plain object", () => {
    const tuple = shipCountsToFleetTuple(new Map([[ShipId.Destroyer, 7]]));
    expect(tuple[9]).toBe(7n);
  });

  it("rejects negative counts", () => {
    expect(() => shipCountsToFleetTuple({ [ShipId.SmallCargo]: -1 })).toThrow(/negative/);
  });

  it("round-trips through fleetTupleToShipCounts", () => {
    const tuple = shipCountsToFleetTuple({ [ShipId.Destroyer]: 5, [ShipId.SmallCargo]: 2 });
    const counts = fleetTupleToShipCounts(tuple);
    expect(counts[ShipId.Destroyer]).toBe(5n);
    expect(counts[ShipId.SmallCargo]).toBe(2n);
    expect(Object.keys(counts)).toHaveLength(2);
  });

  it("fleetTupleToShipCounts rejects a tuple of the wrong length", () => {
    expect(() => fleetTupleToShipCounts([1, 2, 3])).toThrow(/14/);
  });
});
