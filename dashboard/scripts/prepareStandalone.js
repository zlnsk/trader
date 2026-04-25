const fs = require("fs");
const path = require("path");

const root = process.cwd();
const standaloneDir = path.join(root, ".next", "standalone");

function copyDir(source, target) {
  if (!fs.existsSync(source)) {
    throw new Error(`Missing required build artifact: ${path.relative(root, source)}`);
  }
  fs.rmSync(target, { recursive: true, force: true });
  fs.cpSync(source, target, { recursive: true });
}

const serverFile = path.join(standaloneDir, "server.js");
if (!fs.existsSync(serverFile)) {
  throw new Error("Next standalone build did not produce .next/standalone/server.js");
}

copyDir(path.join(root, ".next", "static"), path.join(standaloneDir, ".next", "static"));
copyDir(path.join(root, ".next", "server"), path.join(standaloneDir, ".next", "server"));
copyDir(path.join(root, "public"), path.join(standaloneDir, "public"));

console.log("Prepared standalone runtime artifacts");
