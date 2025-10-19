import { promises as fs } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { execFile } from "child_process";
import { promisify } from "util";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, "..");
const run = promisify(execFile);

const assets = [
  { file: "app/favicon.ico", url: "https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/app/favicon.ico" },
  { file: "public/seo.jpg", url: "https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/public/seo.jpg" },
  { file: "public/github.png", url: "https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/public/github.png" },
  { file: "public/logo.png", url: "https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/public/logo.png" },
  { file: "public/images/cover.png", url: "https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/public/images/cover.png" },
  { file: "public/preview.png", url: "https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/public/preview.png" },
];

for (let i = 1; i <= 12; i += 1) {
  assets.push({
    file: `public/images/avatars/${i}.png`,
    url: `https://raw.githubusercontent.com/bundui/shadcn-admin-dashboard-free/main/public/images/avatars/${i}.png`,
  });
}

async function ensureDir(filePath) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
}

async function fileExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function fetchBuffer(url) {
  if (typeof fetch === "function") {
    try {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`Unexpected response ${response.status} ${response.statusText}`);
      }
      return Buffer.from(await response.arrayBuffer());
    } catch (error) {
      console.warn(`fetch failed for ${url}: ${error.message}`);
    }
  }

  try {
    const { stdout } = await run("curl", ["-fsSL", url], {
      encoding: "buffer",
      maxBuffer: 64 * 1024 * 1024,
    });
    return Buffer.from(stdout);
  } catch (curlError) {
    console.warn(`curl failed for ${url}: ${curlError.message}`);
    return null;
  }
}

async function downloadAsset({ file, url }) {
  const destination = path.join(projectRoot, file);
  if (await fileExists(destination)) {
    return;
  }

  await ensureDir(destination);
  const buffer = await fetchBuffer(url);
  if (!buffer) {
    console.warn(`Skipping ${file} (unable to download)`);
    return;
  }

  await fs.writeFile(destination, buffer);
}

await Promise.all(assets.map(downloadAsset));
console.log("Static assets checked");
