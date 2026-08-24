import { mkdirSync, writeFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";
import { Wallet } from "ethers";
import { input, password as promptPassword } from "@inquirer/prompts";

function expandHome(path) {
  if (path === "~") return homedir();
  if (path.startsWith("~/")) return join(homedir(), path.slice(2));
  return path;
}

const wallet = Wallet.createRandom();
console.log("New address:", wallet.address);

const outDir = await input({
  message: "Output directory:",
  default: "~/.veydrift",
});

const pw = await promptPassword({ message: "Keystore password:", mask: "*" });
const confirm = await promptPassword({ message: "Confirm password:", mask: "*" });
if (pw !== confirm) {
  console.error("Passwords did not match.");
  process.exit(1);
}
if (pw.length === 0) {
  console.error("Password cannot be empty.");
  process.exit(1);
}

console.log("Encrypting (this takes a few seconds)...");
const json = await wallet.encrypt(pw);
const resolvedDir = expandHome(outDir);
mkdirSync(resolvedDir, { recursive: true });
const outPath = join(resolvedDir, "keystore.json");
writeFileSync(outPath, json);
console.log(`Wrote keystore to ${outPath}`);
