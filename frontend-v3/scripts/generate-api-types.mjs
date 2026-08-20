import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(scriptDir, "..");
const rootDir = resolve(frontendDir, "..");
const outputPath = resolve(frontendDir, "src/api/generated/openapi.d.ts");
const tempSchemaPath = resolve(rootDir, ".tooling/openapi-schema.json");
const tempOutputPath = resolve(rootDir, ".tooling/openapi.d.ts");
const pythonPath = process.env.AUDIT_PYTHON ?? resolve(rootDir, ".venv/bin/python");
const openapiCliPath = resolve(frontendDir, "node_modules/openapi-typescript/bin/cli.js");
const checkOnly = process.argv.includes("--check");

mkdirSync(dirname(tempSchemaPath), { recursive: true });
mkdirSync(dirname(outputPath), { recursive: true });

execFileSync(pythonPath, [resolve(rootDir, "scripts/export_openapi.py"), tempSchemaPath], {
  cwd: rootDir,
  stdio: "inherit",
});
execFileSync(process.execPath, [openapiCliPath, tempSchemaPath, "-o", tempOutputPath], {
  cwd: frontendDir,
  stdio: "inherit",
});

const generated = readFileSync(tempOutputPath);
if (checkOnly) {
  let existing = null;
  try {
    existing = readFileSync(outputPath);
  } catch {
    // 由下方统一报出“尚未生成”的明确错误。
  }
  if (!existing || !generated.equals(existing)) {
    console.error("OpenAPI 前端类型已漂移，请执行 pnpm generate:api-types 后提交生成结果。");
    process.exitCode = 1;
  }
} else {
  await import("node:fs/promises").then(({ writeFile }) => writeFile(outputPath, generated));
}

rmSync(tempSchemaPath, { force: true });
rmSync(tempOutputPath, { force: true });
