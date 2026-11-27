import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const projectRoot = resolve(import.meta.dirname, "..");
const args = process.argv.slice(2);
if (args.length > 1 || (args.length === 1 && args[0] !== "--check")) {
  console.error("Usage: npm run vendor [-- --check]");
  process.exit(2);
}
const check = args[0] === "--check";

const assets = [
  ["node_modules/htmx.org/LICENSE", "static/vendor/htmx/LICENSE"],
  ["node_modules/swagger-ui-dist/LICENSE", "static/vendor/swagger-ui/LICENSE"],
  ["node_modules/swagger-ui-dist/swagger-ui-bundle.js.LICENSE.txt", "static/vendor/swagger-ui/swagger-ui-bundle.js.LICENSE.txt"],
  ["node_modules/highlight.js/LICENSE", "static/vendor/highlight/LICENSE"],
  ["node_modules/@fontsource/space-grotesk/LICENSE", "static/vendor/fonts/space-grotesk-LICENSE.txt"],
  ["node_modules/@fontsource/inter/LICENSE", "static/vendor/fonts/inter-LICENSE.txt"],
  ["node_modules/@fontsource/jetbrains-mono/LICENSE", "static/vendor/fonts/jetbrains-mono-LICENSE.txt"],
  ["tokens.css", "static/css/tokens.css"],
  ["node_modules/htmx.org/dist/htmx.min.js", "static/vendor/htmx/htmx.min.js"],
  ["node_modules/swagger-ui-dist/swagger-ui-bundle.js", "static/vendor/swagger-ui/swagger-ui-bundle.js"],
  ["node_modules/swagger-ui-dist/swagger-ui.css", "static/vendor/swagger-ui/swagger-ui.css"],
  ["node_modules/highlight.js/styles/github-dark.min.css", "static/vendor/highlight/github-dark.min.css"],
  [
    "node_modules/@fontsource/space-grotesk/files/space-grotesk-latin-500-normal.woff2",
    "static/vendor/fonts/space-grotesk-latin-500-normal.woff2",
  ],
  [
    "node_modules/@fontsource/space-grotesk/files/space-grotesk-latin-700-normal.woff2",
    "static/vendor/fonts/space-grotesk-latin-700-normal.woff2",
  ],
  [
    "node_modules/@fontsource/inter/files/inter-latin-400-normal.woff2",
    "static/vendor/fonts/inter-latin-400-normal.woff2",
  ],
  [
    "node_modules/@fontsource/inter/files/inter-latin-600-normal.woff2",
    "static/vendor/fonts/inter-latin-600-normal.woff2",
  ],
  [
    "node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-400-normal.woff2",
    "static/vendor/fonts/jetbrains-mono-latin-400-normal.woff2",
  ],
  [
    "node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-600-normal.woff2",
    "static/vendor/fonts/jetbrains-mono-latin-600-normal.woff2",
  ],
  [
    "node_modules/@fontsource/noto-sans-symbols/files/noto-sans-symbols-symbols-400-normal.woff2",
    "static/vendor/fonts/noto-sans-symbols-symbols-400-normal.woff2",
  ],
  [
    "node_modules/@fontsource/noto-sans-symbols/LICENSE",
    "static/vendor/fonts/noto-sans-symbols-LICENSE.txt",
  ],
];

for (const [source, destination] of assets) {
  const bytes = await readFile(resolve(projectRoot, source));
  const expected = destination === "static/vendor/swagger-ui/swagger-ui.css"
    ? Buffer.from(bytes.toString("utf8").replace(/\n?\/\*# sourceMappingURL=swagger-ui\.css\.map\*\//u, ""))
    : bytes;
  const destinationPath = resolve(projectRoot, destination);
  if (check) {
    const actual = await readFile(destinationPath).catch((error) => {
      if (error.code === "ENOENT") return null;
      throw error;
    });
    if (!actual?.equals(expected)) {
      console.error(`Vendor asset differs from its source: ${destination}`);
      process.exitCode = 1;
    }
    continue;
  }
  await mkdir(resolve(destinationPath, ".."), { recursive: true });
  await writeFile(destinationPath, expected);
}
