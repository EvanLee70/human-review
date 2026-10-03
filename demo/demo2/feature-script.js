const fs = require("fs");
const path = require("path");
const {execSync} = require("child_process");
const ts = require("typescript");

const ROOT = process.cwd();
const APP_DIR = path.join(ROOT, "petclinic-frontend/src/app");

const walk = dir => fs.readdirSync(dir, {withFileTypes: true}).flatMap(d =>
  d.isDirectory() ? walk(path.join(dir, d.name)) : [path.join(dir, d.name)]);

const parse = file => ts.createSourceFile(file, fs.readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
const propName = p => p.name && (ts.isIdentifier(p.name) || ts.isStringLiteralLike(p.name)) ? p.name.text : null;
const prop = (obj, name) => obj.properties.find(p => ts.isPropertyAssignment(p) && propName(p) === name);
const str = n => n && ts.isStringLiteralLike(n.initializer) ? n.initializer.text : null;

// Components: class name -> {file, selector, template}; routes: [{route, component}]
function scanApp() {
  const files = walk(APP_DIR).filter(f => f.endsWith(".ts") && !f.endsWith(".spec.ts"));
  const components = [];
  const routes = [];
  for (const file of files) {
    const sf = parse(file);
    const visitedArrays = new Set();
    const readRoutes = (arr, prefix) => {
      visitedArrays.add(arr);
      for (const el of arr.elements) {
        if (!ts.isObjectLiteralExpression(el)) continue;
        const p = prop(el, "path");
        if (!p || !ts.isStringLiteralLike(p.initializer)) continue;
        const full = [prefix, p.initializer.text].filter(Boolean).join("/");
        const comp = prop(el, "component");
        if (comp && ts.isIdentifier(comp.initializer)) routes.push({route: "/" + full, component: comp.initializer.text});
        const ch = prop(el, "children");
        if (ch && ts.isArrayLiteralExpression(ch.initializer)) readRoutes(ch.initializer, full);
      }
    };
    const visit = node => {
      if (ts.isClassDeclaration(node) && node.name) {
        const decorators = (ts.canHaveDecorators(node) ? ts.getDecorators(node) : []) || [];
        for (const d of decorators) {
          if (!ts.isCallExpression(d.expression) || d.expression.expression.getText() !== "Component") continue;
          const arg = d.expression.arguments[0];
          if (!arg || !ts.isObjectLiteralExpression(arg)) continue;
          const templateUrl = str(prop(arg, "templateUrl"));
          const inline = str(prop(arg, "template"));
          const template = templateUrl ? path.resolve(path.dirname(file), templateUrl) : null;
          components.push({
            name: node.name.text, file, selector: str(prop(arg, "selector")), templateFile: template,
            html: template && fs.existsSync(template) ? fs.readFileSync(template, "utf8") : (inline || "")
          });
        }
      }
      if (ts.isArrayLiteralExpression(node) && !visitedArrays.has(node)) {
        const looksLikeRoutes = node.elements.some(e => ts.isObjectLiteralExpression(e) && prop(e, "path"));
        if (looksLikeRoutes) readRoutes(node, "");
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }
  return {components, routes};
}

// Audited base of this review; merge-base keeps it right if HEAD moves on
const base = () => execSync("git merge-base 5a97353e HEAD", {cwd: ROOT, encoding: "utf8"}).trim();

function changedFrontendFiles() {
  return execSync(`git diff --name-only ${base()} HEAD -- petclinic-frontend/src`, {cwd: ROOT, encoding: "utf8"})
    .split("\n").map(s => s.trim()).filter(Boolean).map(f => path.join(ROOT, f));
}

// changed components -> routes rendering them, climbing through EVERY ancestor that embeds them
function deriveScreens(changed = changedFrontendFiles()) {
  const {components, routes} = scanApp();
  const changedSet = new Set(changed);
  const seeds = components.filter(c => changedSet.has(c.file) || (c.templateFile && changedSet.has(c.templateFile)));
  const embeds = (parent, child) => child.selector &&
    new RegExp(`<${child.selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[\\s>/]`).test(parent.html);
  const reached = new Map(seeds.map(c => [c.name, c]));
  const queue = [...seeds];
  while (queue.length) {
    const child = queue.shift();
    for (const parent of components) {
      if (!reached.has(parent.name) && embeds(parent, child)) { reached.set(parent.name, parent); queue.push(parent); }
    }
  }
  const screens = [];
  for (const r of routes) if (reached.has(r.component) && !screens.some(s => s.route === r.route)) screens.push(r);
  return {screens, seeds: seeds.map(c => c.name)};
}

module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  const {screens, seeds} = deriveScreens();
  const visited = [], missed = [], unfilmable = [];

  const range = () => page.locator(".mat-mdc-paginator-range-label");
  const dash = "\\s*[–-]\\s*";
  const rangeRe = (from, to, total = "\\d+") => new RegExp(`${from}${dash}${to}\\s+of\\s+${total}`);
  const settled = async (re) => {
    await range().filter({hasText: re}).waitFor();
    await page.locator('#ownersTable table[aria-busy="false"]').waitFor();
  };
  const header = col => page.locator(`#ownersTable th[mat-sort-header="${col}"]`);
  const sortedBy = (col, dir) =>
    page.locator(`#ownersTable th[mat-sort-header="${col}"][aria-sort="${dir}"]`);
  const search = async (text) => {
    await page.locator("#lastName").fill(text);
    await page.locator('#search-owner-form button[type="submit"]').click();
  };

  const ownersHandler = async () => {
    await page.goto(`${app}/owners`);
    await settled(rangeRe(1, 10));
    await page.locator("#addOwner").waitFor();
    const paginator = page.locator("mat-paginator");
    await say("The Owners list is now paginated by the server, one page at a time.", paginator);
    await sortedBy("name", "ascending").waitFor();
    await say("It opens on page one, ten owners, sorted by Name.", header("name"));
    await say("The range shows how many owners match in all.", range());
    await pause(1500);

    const next = page.locator(".mat-mdc-paginator-navigation-next");
    await next.waitFor();
    await next.click();
    await settled(rangeRe(11, 20));
    await say("Next page shows owners eleven to twenty.", range());

    const sizeSelect = page.locator(".mat-mdc-paginator-page-size-select");
    await sizeSelect.waitFor();
    await sizeSelect.click();
    const option5 = page.locator("mat-option").filter({hasText: /^\s*5\s*$/});
    const option20 = page.locator("mat-option").filter({hasText: /^\s*20\s*$/});
    await option5.waitFor();
    await option20.waitFor();
    await say("Page sizes are five, ten or twenty.", option20);
    await option20.click();
    await settled(rangeRe(1, 20));
    await say("Changing the page size restarts from page one.", range());

    await header("name").waitFor();
    await header("city").waitFor();
    await say("Name and City are the sortable columns.", header("city"));
    await header("city").click();
    await sortedBy("city", "ascending").waitFor();
    await settled(rangeRe(1, 20));
    await say("Clicking City sorts ascending, on the server.", header("city"));
    await header("city").click();
    await sortedBy("city", "descending").waitFor();
    await settled(rangeRe(1, 20));
    await say("Clicking again reverses it.", header("city"));

    await search("Pot");
    await settled(rangeRe(1, 2, "2"));
    await sortedBy("city", "descending").waitFor();
    const beatrix = page.locator("#ownersTable .ownerFullName").filter({hasText: "Beatrix Potter"});
    const harry = page.locator("#ownersTable .ownerFullName").filter({hasText: "Harry Potter"});
    await beatrix.waitFor();
    await harry.waitFor();
    await say("A search goes back to page one and keeps the City sort.", range());
    await say("Harry and Beatrix Potter are the two matches.", harry);

    await search("Zzzz");
    const noOwners = page.locator("#noOwners");
    await noOwners.waitFor();
    await paginator.waitFor({state: "detached"});
    await say("With no match, a message replaces the grid and the paginator.", noOwners);

    await search("");
    await settled(/of\s+\d+/);
    await page.locator("#ownersTable").waitFor();
    await say("An empty search brings every owner back, from page one.", range());
    await pause(1000);
  };

  const plainHandler = async (screen) => {
    await page.goto(`${app}${screen.route}`);
    const h2 = page.locator("h2").first();
    await h2.waitFor();
    await say("This screen also contains the changed component.", h2);
  };

  for (const screen of screens) {
    if (screen.route.includes(":")) {
      unfilmable.push(`${screen.route} (route needs a URL parameter)`);
      continue;
    }
    try {
      await (screen.route === "/owners" ? ownersHandler : plainHandler)(screen);
      visited.push(screen.route);
    } catch (e) {
      missed.push(`${screen.route} (${e.message.split("\n")[0]})`);
    }
  }
  if (!screens.length) missed.push(`no routed screen derived from changed components [${seeds.join(", ")}]`);

  return {
    ok: missed.length === 0 && unfilmable.length === 0,
    note: `${visited.length}/${screens.length} changed screens filmed: ${visited.join(", ")}`
      + (unfilmable.length ? ` | not filmable: ${unfilmable.join("; ")}` : "")
      + (missed.length ? ` | FAILED to reach: ${missed.join("; ")}` : "")
  };
};
module.exports.deriveScreens = deriveScreens;
