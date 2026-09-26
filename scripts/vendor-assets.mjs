import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const projectRoot = resolve(import.meta.dirname, "..");

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
  const sourcePath = resolve(projectRoot, source);
  const destinationPath = resolve(projectRoot, destination);
  await mkdir(resolve(destinationPath, ".."), { recursive: true });
  await copyFile(sourcePath, destinationPath);
}

const swaggerCssPath = resolve(projectRoot, "static/vendor/swagger-ui/swagger-ui.css");
const swaggerCss = await readFile(swaggerCssPath, "utf8");
await writeFile(
  swaggerCssPath,
  swaggerCss.replace(/\n?\/\*# sourceMappingURL=swagger-ui\.css\.map\*\//u, ""),
  "utf8",
);
