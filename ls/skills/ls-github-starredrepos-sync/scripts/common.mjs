import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const REST_VERSION = process.env.STARREDREPOS_REST_API_VERSION || "2026-03-10";
export const GITHUB_HOST = process.env.STARREDREPOS_GITHUB_HOST || process.env.GH_HOST || "github.com";
export const REPO_NAME = process.env.STARREDREPOS_REPO_NAME || "starredrepos";

export function githubEnv(env = process.env) {
  return { ...env, GH_HOST: GITHUB_HOST };
}

export function here(importMetaUrl) {
  return dirname(fileURLToPath(importMetaUrl));
}

export function isoNow() {
  return new Date().toISOString();
}

export function usageAndExit(text) {
  process.stdout.write(`${text}\n`);
  process.exit(0);
}

export function parseCommonArgs(argv, allowed) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--help" || arg === "-h") {
      options.help = true;
      continue;
    }
    const spec = allowed[arg];
    if (!spec) {
      throw new Error(`Unknown argument: ${arg}`);
    }
    if (spec === "boolean") {
      options[arg.slice(2).replaceAll("-", "_")] = true;
      continue;
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new Error(`Missing value for ${arg}`);
    }
    options[arg.slice(2).replaceAll("-", "_")] = value;
    index += 1;
  }
  return options;
}

export async function run(command, args, options = {}) {
  const timeoutMs = options.timeoutMs ?? 30000;
  return await new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`Command timed out after ${timeoutMs}ms: ${command} ${args.join(" ")}`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(`Command failed (${code}): ${command} ${args.join(" ")}\n${redact(stderr)}`));
        return;
      }
      resolvePromise({ stdout, stderr });
    });
    if (options.input) {
      child.stdin.write(options.input);
    }
    child.stdin.end();
  });
}

async function gitWorktreeRoot(worktree) {
  const result = await run("git", ["rev-parse", "--show-toplevel"], {
    cwd: worktree,
    timeoutMs: 30000,
  });
  return resolve(result.stdout.trim());
}

export async function ensureGitRepository(worktree) {
  const expectedRoot = resolve(worktree);
  let root;
  try {
    root = await gitWorktreeRoot(worktree);
  } catch (error) {
    if (!String(error.message).includes("not a git repository")) {
      throw error;
    }
  }
  if (root) {
    if (root !== expectedRoot) {
      throw new Error("Archive worktree must be the Git repository root: " + expectedRoot);
    }
    return false;
  }

  await run("git", ["init"], { cwd: worktree, timeoutMs: 30000 });
  const initializedRoot = await gitWorktreeRoot(worktree);
  if (initializedRoot !== expectedRoot) {
    throw new Error("Git initialization did not create the requested archive worktree: " + expectedRoot);
  }
  return true;
}

export async function ghApi(path, extraArgs = []) {
  const args = [
    "api",
    "--hostname",
    GITHUB_HOST,
    "-H",
    `X-GitHub-Api-Version: ${REST_VERSION}`,
    ...extraArgs,
    path,
  ];
  const result = await run("gh", args, { timeoutMs: 30000 });
  return parseJson(result.stdout, `gh api ${path}`);
}

export async function ghApiRaw(path, extraArgs = []) {
  const args = [
    "api",
    "--hostname",
    GITHUB_HOST,
    "-H",
    `X-GitHub-Api-Version: ${REST_VERSION}`,
    ...extraArgs,
    path,
  ];
  return await run("gh", args, { timeoutMs: 30000 });
}

export function parseJson(raw, label) {
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`Invalid JSON from ${label}: ${error.message}`);
  }
}

export async function readJson(path) {
  return parseJson(await readFile(path, "utf8"), path);
}

export async function writeJson(path, payload) {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

export function redact(value) {
  return String(value)
    .replace(/github_pat_[A-Za-z0-9_]+/g, "[REDACTED_TOKEN]")
    .replace(/gh[pousr]_[A-Za-z0-9_]+/g, "[REDACTED_TOKEN]")
    .replace(/Bearer\s+[A-Za-z0-9_.-]+/gi, "Bearer [REDACTED]");
}

export function assertSafeFullName(fullName, label = "fullName") {
  if (typeof fullName !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(fullName)) {
    throw new Error(`Invalid ${label}: must be exactly owner/name using safe GitHub name characters`);
  }
  const [owner, name] = fullName.split("/");
  if ([owner, name].includes(".") || [owner, name].includes("..")) {
    throw new Error(`Invalid ${label}: owner and name must not be path traversal segments`);
  }
}

export function repoDocFileName(fullName) {
  assertSafeFullName(fullName);
  return `${fullName.replace("/", "__")}.md`;
}

export function normalizeRepo(repo, starredAt = null, fetchedAt = isoNow()) {
  const owner = repo.owner?.login || repo.owner || "";
  return {
    fullName: repo.full_name || `${owner}/${repo.name}`,
    owner,
    name: repo.name,
    htmlUrl: repo.html_url,
    cloneUrl: repo.clone_url,
    defaultBranch: repo.default_branch || "main",
    description: repo.description ?? null,
    language: repo.language ?? null,
    topics: Array.isArray(repo.topics) ? repo.topics : [],
    license: repo.license?.spdx_id || repo.license?.key || null,
    visibility: repo.visibility || (repo.private ? "private" : "public"),
    archived: Boolean(repo.archived),
    fork: Boolean(repo.fork),
    pushedAt: repo.pushed_at ?? null,
    starredAt,
    stargazersCount: Number(repo.stargazers_count || 0),
    openIssuesCount: Number(repo.open_issues_count || 0),
    metadataFetchedAt: fetchedAt,
  };
}

function parseLinkHeader(headerText) {
  const linkLine = headerText.split(/\r?\n/).find((line) => line.toLowerCase().startsWith("link:"));
  if (!linkLine) {
    return null;
  }
  const match = /<([^>]+)>;\s*rel="next"/.exec(linkLine);
  return match ? match[1] : null;
}

function pathFromNextUrl(url) {
  const parsed = new URL(url);
  return `${parsed.pathname}${parsed.search}`;
}

export async function fetchStarredRepos(limit = 10000) {
  const fetchedAt = isoNow();
  const repos = [];
  let path = "/user/starred?per_page=100";
  while (path && repos.length < limit) {
    const result = await ghApiRaw(path, [
      "-i",
      "-H",
      "Accept: application/vnd.github.star+json",
    ]);
    const splitIndex = result.stdout.indexOf("\r\n\r\n");
    const altSplitIndex = result.stdout.indexOf("\n\n");
    const index = splitIndex >= 0 ? splitIndex : altSplitIndex;
    const headers = index >= 0 ? result.stdout.slice(0, index) : "";
    const body = index >= 0 ? result.stdout.slice(index).trimStart() : result.stdout;
    const page = parseJson(body, "starred repositories page");
    if (!Array.isArray(page)) {
      throw new Error("Expected starred repository page to be an array");
    }
    for (const item of page) {
      const repo = item.repo || item;
      repos.push(normalizeRepo(repo, item.starred_at || null, fetchedAt));
      if (repos.length >= limit) {
        break;
      }
    }
    const next = parseLinkHeader(headers);
    path = next && repos.length < limit ? pathFromNextUrl(next) : null;
  }
  return repos;
}

export function validateRepoMetadata(repo, label = "repo metadata") {
  const required = ["fullName", "owner", "name", "htmlUrl", "cloneUrl", "defaultBranch"];
  for (const key of required) {
    if (typeof repo[key] !== "string" || repo[key].length === 0) {
      throw new Error(`Invalid ${label}: ${key} must be a non-empty string`);
    }
  }
  assertSafeFullName(repo.fullName, `${label}.fullName`);
  if (!Array.isArray(repo.topics)) {
    throw new Error(`Invalid ${label}: topics must be an array`);
  }
  for (const key of ["archived", "fork"]) {
    if (typeof repo[key] !== "boolean") {
      throw new Error(`Invalid ${label}: ${key} must be boolean`);
    }
  }
}

export function validateManifest(manifest) {
  if (manifest.schemaVersion !== "1.0") {
    throw new Error("Manifest schemaVersion must be 1.0");
  }
  if (!Array.isArray(manifest.repositories)) {
    throw new Error("Manifest repositories must be an array");
  }
  for (const repo of manifest.repositories) {
    validateRepoMetadata(repo, `repo ${repo.fullName || "(unknown)"}`);
  }
  if (manifest.repositoryCount !== manifest.repositories.length) {
    throw new Error("Manifest repositoryCount must match repositories length");
  }
}

export function validateScoutReport(report) {
  if (report.schemaVersion !== "1.0") {
    throw new Error("Scout report schemaVersion must be 1.0");
  }
  if (!["static", "command"].includes(report.mode)) {
    throw new Error("Scout report mode must be static or command");
  }
  assertSafeFullName(report.fullName, "scout report fullName");
  if (typeof report.summary !== "string") {
    throw new Error("Scout report summary must be a string");
  }
  if (!Array.isArray(report.claims)) {
    throw new Error("Scout report claims must be an array");
  }
}

export function defaultWorktree() {
  return process.env.STARREDREPOS_WORKTREE || resolve(process.env.HOME || process.cwd(), REPO_NAME);
}
