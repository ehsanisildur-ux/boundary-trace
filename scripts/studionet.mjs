import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { setTimeout as sleep } from "node:timers/promises";
import { createAccount, createClient } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { keccak256 } from "viem";

const verify = process.argv.includes("--verify");
const resume = process.argv.includes("--resume");
const journal = verify ? "proofs/studionet.json" : "proofs/work/proof.json";
const endpoint = "https://studio.genlayer.com/api";
const client = createClient({ chain: studionet });
const contractCode = await readFile("contracts/boundary_trace.py");
const fixture = JSON.parse(await readFile("fixtures/colorado.geojson", "utf8"));
const source = "https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS2025/State_County/MapServer/9/query?where=STATE%3D%2708%27&outFields=STATE%2CNAME&returnGeometry=true&outSR=4326&geometryPrecision=5&f=geojson";
const digest = value => createHash("sha256").update(value).digest("hex");
const sourceHash = digest(contractCode);
const route = [[-11000000, 3900000], [-10500000, 3900000], [-10100000, 3900000]];
const txPlan = [
  { label: "deploy" },
  { label: "observe-colorado", method: "observe" },
  { label: "repeat-observation", method: "observe" },
];

async function rpc(method, params = []) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    signal: AbortSignal.timeout(25_000),
  });
  assert(response.ok, `RPC HTTP ${response.status}`);
  const result = await response.json();
  assert(!result.error, result.error?.message);
  return result.result;
}

async function read(operation) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await operation();
    } catch (error) {
      if (attempt === 3 || !/429|50[0234]|internal|timeout|fetch failed|temporarily|abort/i.test(String(error))) throw error;
      await sleep(3000 * (attempt + 1));
    }
  }
}

function canonical(value) {
  if (typeof value === "bigint") return Number(value);
  if (Array.isArray(value)) return value.map(canonical);
  if (value instanceof Map) return canonical(Object.fromEntries(value));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}

function expectedSnapshot() {
  const feature = fixture.features[0];
  assert.equal(feature.properties.STATE, "08");
  assert.equal(feature.geometry.type, "Polygon");
  assert.equal(feature.geometry.coordinates.length, 1);
  const ring = feature.geometry.coordinates[0].slice(0, -1).map(point => point.map(value => Math.round(value * 100000)));
  const comparePoint = (a, b) => a[0] - b[0] || a[1] - b[1];
  const first = ring.indexOf([...ring].sort(comparePoint)[0]);
  const ordered = [...ring.slice(first), ...ring.slice(0, first)];
  const forward = comparePoint(ordered[1], ordered.at(-1)) <= 0;
  return canonical({ source, state_fips: "08", scale: 100000, rings: [
    forward ? ordered : [ordered[0], ...ordered.slice(1).reverse()],
  ] });
}

async function finalized(hash) {
  for (let attempt = 0; attempt < 95; attempt++) {
    let receipt;
    try {
      receipt = await rpc("eth_getTransactionByHash", [hash]);
    } catch (error) {
      if (attempt === 94) throw error;
    }
    const status = receipt?.status_name ?? receipt?.status;
    if (status === "FINALIZED") return receipt;
    assert.notEqual(status, "CANCELED");
    await sleep(5000);
  }
  throw new Error(`Finalization timed out for ${hash}`);
}

function checkReceipt(receipt, entry, address) {
  assert.equal(receipt.hash ?? receipt.tx_id, entry.hash);
  assert.equal((receipt.to_address ?? receipt.recipient).toLowerCase(), address.toLowerCase());
  assert.equal((receipt.from_address ?? receipt.sender).toLowerCase(), entry.sender.toLowerCase());
  assert.equal(receipt.result_name ?? receipt.result, "MAJORITY_AGREE");
  const leader = receipt.consensus_data.leader_receipt[0];
  const votes = Object.values(receipt.consensus_data.votes);
  const agree = votes.filter(vote => vote === "agree").length;
  assert(agree > votes.length / 2);
  assert(["SUCCESS", "FINISHED_WITH_RETURN"].includes(leader.execution_result));
  return { status: "FINALIZED", execution: leader.execution_result, agree, votes: votes.length };
}

assert.equal(await read(() => rpc("eth_chainId")), "0xf22f", "StudioNet only");
if (!verify) assert.equal(BigInt(await read(() => rpc("eth_gasPrice"))), 0n);
const prior = verify || resume ? JSON.parse(await readFile(journal, "utf8")) : null;
const revision = prior?.revision ?? process.env.BOUNDARY_TRACE_REVISION;
assert.match(revision, /^[a-f0-9]{40}$/);
const snapshot = expectedSnapshot();
const snapshotId = digest(JSON.stringify(snapshot));
const ledger = prior ?? { network: "studionet", revision, source_sha256: sourceHash, snapshot_id: snapshotId, transactions: [] };
assert.equal(ledger.source_sha256, sourceHash);
assert.equal(ledger.snapshot_id, snapshotId);
const save = () => writeFile(journal, JSON.stringify(ledger, null, 2) + "\n");
if (!prior) {
  await mkdir("proofs/work", { recursive: true });
  await writeFile(journal, JSON.stringify(ledger, null, 2) + "\n", { flag: "wx" });
}
if (verify) assert.deepEqual(ledger.transactions.map(entry => entry.label), txPlan.map(step => step.label));

for (const step of txPlan) {
  let entry = ledger.transactions.find(item => item.label === step.label);
  if (entry?.evm_hash && !entry.hash) {
    const receipt = await read(() => rpc("eth_getTransactionReceipt", [entry.evm_hash]));
    assert.equal(receipt?.status, "0x1", `Inspect the previously signed transaction ${entry.evm_hash}`);
    entry.hash = entry.evm_hash;
  }
  if (!entry?.hash) {
    assert(!verify, "Proof is missing a transaction");
    const account = createAccount();
    await rpc("sim_fundAccount", [account.address, 1e18]);
    const balance = BigInt(await read(() => rpc("eth_getBalance", [account.address, "latest"])));
    assert(balance >= 1_000_000_000_000_000_000n);
    if (!entry) {
      entry = { label: step.label };
      ledger.transactions.push(entry);
    }
    entry.sender = account.address;
    const signer = {
      ...account,
      async signTransaction(transaction) {
        assert.equal(transaction.chainId, studionet.id);
        assert(BigInt(transaction.value ?? 0) <= 250_000_000_000_000_000n);
        const signed = await account.signTransaction(transaction);
        entry.evm_hash = keccak256(signed);
        await save();
        return signed;
      },
    };
    const sender = createClient({ chain: studionet, account: signer });
    const budget = step.method ? { executionBudgetPerRound: 125_000_000_000_000_000n } : {};
    const quote = await read(() => sender.estimateTransactionFees({
      leaderTimeunitsAllocation: 100n, validatorTimeunitsAllocation: 200n, rotations: [0], ...budget,
    }));
    assert(quote.feeValue <= 250_000_000_000_000_000n);
    entry.test_fee_deposit_wei = quote.feeValue.toString();
    await save();
    const options = { fees: { distribution: quote.distribution, feeValue: quote.feeValue }, consensusMaxRotations: 0 };
    const submit = () => step.method
      ? sender.writeContract({
        ...options, address: ledger.contract, functionName: step.method, args: [],
        value: 0n,
      })
      : sender.deployContract({ ...options, code: contractCode, args: [] });
    entry.hash = await read(async () => {
      assert(!entry.evm_hash, `Inspect the previously signed transaction ${entry.evm_hash}`);
      return submit();
    });
    assert.match(entry.hash, /^0x[\da-f]{64}$/i);
    await save();
    console.log("Submitted", step.label, entry.hash);
  }
  const receipt = await finalized(entry.hash);
  if (!step.method) {
    const deployed = receipt.to_address ?? receipt.recipient;
    if (ledger.contract) assert.equal(ledger.contract, deployed);
    ledger.contract = deployed;
    assert.deepEqual(Buffer.from(receipt.data.contract_code, "base64"), contractCode);
    assert.equal(await read(() => client.getContractCode(deployed)), contractCode.toString("utf8"));
  }
  const checked = checkReceipt(receipt, entry, ledger.contract);
  if (step.method) {
    checked.return_data = receipt.consensus_data.leader_receipt[0].result;
    assert.equal(typeof checked.return_data, "string");
  }
  if (verify) {
    for (const [key, value] of Object.entries(checked)) assert.equal(entry[key], value);
  } else {
    Object.assign(entry, checked);
    await save();
  }
  console.log("Finalized", step.label, checked.execution, `${checked.agree}/${checked.votes}`);
}

const view = (functionName, args) => read(() => client.readContract({ address: ledger.contract, functionName, args })).then(canonical);
assert.equal(ledger.transactions[1].return_data, ledger.transactions[2].return_data);
const observed = await view("get_snapshot", [snapshotId]);
assert.deepEqual(observed, snapshot);
const trace = await view("trace", [snapshotId, Buffer.from(JSON.stringify(route))]);
const west = [5927555893, 31303500000];
const east = [688117091, 931920000];
assert.deepEqual(trace, canonical({ snapshot_id: snapshotId, route, segments: [
  { intervals: [
    { from: [0, 1], to: west, kind: "OUTSIDE" },
    { from: west, to: [1, 1], kind: "INSIDE" },
  ], contacts: [west] },
  { intervals: [
    { from: [0, 1], to: east, kind: "INSIDE" },
    { from: east, to: [1, 1], kind: "OUTSIDE" },
  ], contacts: [east] },
] }));
assert.equal(await view("get_snapshot", ["0".repeat(64)]), null);
const result = { snapshot: observed, trace };
if (verify) {
  assert.deepEqual(canonical(ledger.readback), canonical(result));
} else {
  ledger.readback = result;
  await save();
}
console.log("Verified", ledger.contract, snapshotId);
