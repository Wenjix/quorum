import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Resolve the packaged scripts/ directory from a module's import.meta.url.
 * cli.js lives at dist/src/cli.js, so the package root is three levels up.
 *
 * Uses fileURLToPath rather than a raw string replace so Windows drive letters
 * (file:///C:/...) and percent-encoded characters (e.g. a space in the install
 * path, which arrives as %20) resolve to a real on-disk path.
 */
export function scriptsDirFromModuleUrl(url: string): string {
  if (url.startsWith("file://")) {
    const filePath = fileURLToPath(url);
    return join(dirname(dirname(dirname(filePath))), "scripts");
  }
  return join(process.cwd(), "scripts");
}
