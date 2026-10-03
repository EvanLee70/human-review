const fs = require("fs");
const path = require("path");
const {execSync} = require("child_process");
const ts = require("typescript");

const APP_DIR = "petclinic-frontend/src/app";

const walk = d => fs.readdirSync(d, {withFileTypes: true}).flatMap(e =>
  e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]);

const baseRef = () => {
  try { return JSON.parse(fs.readFileSync("human-review.json", "utf8")).base || "origin/main"; }
  catch { return "origin/main"; }
};

const parse = f => ts.createSourceFile(f, fs.readFileSync(f, "utf8"), ts.ScriptTarget.Latest, true);

// every @Component class: {cls, selector, file, html}
function components(files) {
  const found = [];
  for (const f of files.filter(f => f.endsWith(".component.ts"))) {
    const visit = n => {
      if (ts.isClassDeclaration(n) && n.name) {
        const deco = (ts.getDecorators ? ts.getDecorators(n) : n.decorators) || [];
        for (const d of deco) {
          const call = d.expression;
          if (!ts.isCallExpression(call) || call.expression.getText() !== "Component") continue;
          const props = {};
          const obj = call.arguments[0];
          if (obj && ts.isObjectLiteralExpression(obj))
            for (const p of obj.properties)
              if (ts.isPropertyAssignment(p) && ts.isStringLiteralLike(p.initializer))
                props[p.name.getText()] = p.initializer.text;
          const html = props.templateUrl ? path.join(path.dirname(f), props.templateUrl) : null;
          found.push({cls: n.name.text, selector: props.selector, file: f, html});
        }
      }
      ts.forEachChild(n, visit);
    };
    visit(parse(f));
  }
  return found;
}

// {path, cls} from every `{path: '...', component: X}` object literal
function routes(files) {
  const found = [];
  for (const f of files.filter(f => f.endsWith(".ts") && !f.endsWith(".spec.ts"))) {
    const visit = n => {
      if (ts.isObjectLiteralExpression(n)) {
        let p, c;
        for (const prop of n.properties) {
          if (!ts.isPropertyAssignment(prop)) continue;
          const name = prop.name.getText();
          if (name === "path" && ts.isStringLiteralLike(prop.initializer)) p = prop.initializer.text;
          if (name === "component" && ts.isIdentifier(prop.initializer)) c = prop.initializer.text;
        }
        if (p !== undefined && c) found.push({path: p, cls: c});
      }
      ts.forEachChild(n, visit);
    };
    visit(parse(f));
  }
  return found;
}

// changed components -> routed ancestors, climbing past every routed one
function deriveScreens() {
  const files = walk(APP_DIR);
  const comps = components(files);
  const routeTable = routes(files);
  const changed = execSync(`git diff --name-only ${baseRef()}...HEAD -- ${APP_DIR}`, {encoding: "utf8"})
    .split("\n").filter(Boolean).filter(f => !/\.spec\.ts$|generated/.test(f));
  const seen = new Set();
  const queue = comps.filter(c => changed.some(f => f === c.file || f === c.html
    || path.dirname(f) === path.dirname(c.file) && /\.(css|scss)$/.test(f)));
  while (queue.length) {
    const c = queue.shift();
    if (seen.has(c.cls)) continue;
    seen.add(c.cls);
    for (const parent of comps)
      if (parent.html && c.selector && fs.readFileSync(parent.html, "utf8").includes(`<${c.selector}`)) queue.push(parent);
  }
  const screens = routeTable.filter(r => seen.has(r.cls)).map(r => ({route: "/" + r.path, cls: r.cls}));
  return {screens, changedComponents: [...seen]};
}

module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  const rangeLabel = page.locator(".mat-mdc-paginator-range-label");
  const nextButton = page.locator(".mat-mdc-paginator-navigation-next");
  const firstNames = () => page.locator("#ownersTable td.ownerFullName").allTextContents();
  const settled = async () => {
    await page.locator("#ownersTable[aria-busy='false']").waitFor();
  };
  const sortHeader = label => page.locator("#ownersTable th[mat-sort-header]").filter({hasText: label});
  const search = async lastName => {
    await page.locator("#lastName").fill(lastName);
    await page.locator("#search-owner-form button[type='submit']").click();
  };
  const changeRange = async action => {
    const before = await rangeLabel.textContent();
    await action();
    await page.waitForFunction(
      ([sel, prev]) => document.querySelector(sel)?.textContent !== prev,
      [".mat-mdc-paginator-range-label", before]);
    await settled();
  };
  const clickSort = async label => {
    const header = sortHeader(label);
    const before = await header.getAttribute("aria-sort");
    await header.click();
    for (let i = 0; i < 50 && await header.getAttribute("aria-sort") === before; i++) await pause(100);
    await settled();
  };

  const handlers = {
    "/owners": async ({route}) => {
      await page.goto(`${app}${route}`);
      const table = page.locator("#ownersTable");
      await table.waitFor();
      await settled();
      await rangeLabel.waitFor();
      await say("The Owners grid now shows one page at a time, ten owners to start with.", table);
      await pause(1500);
      await say("A paginator under the grid shows the range and the total, fetched from the server.", rangeLabel);
      await pause(1500);

      await changeRange(() => nextButton.click());
      await say("Next page: the server returns the following slice, and the range moves on.", rangeLabel);
      await pause(1500);

      await page.locator(".mat-mdc-paginator-page-size-select").click();
      await page.locator("mat-option").filter({hasText: /^\s*5\s*$/}).waitFor();
      await say("The page size is selectable: five, ten or twenty.", page.locator(".mat-mdc-select-panel"));
      await changeRange(() => page.locator("mat-option").filter({hasText: /^\s*5\s*$/}).click());
      await say("Changing the size starts again from the first page.", rangeLabel);
      await pause(1500);

      const nameHeader = sortHeader("Name");
      await nameHeader.waitFor();
      await say("Name is the default sort, ascending, by last name.", nameHeader);
      await clickSort("Name");
      await say("Sort by Name again, now descending. Sorting returns to page one.", nameHeader);
      await pause(1500);
      const sample = (await firstNames())[0];
      await say(`The server now returns ${sample} first.`, page.locator("#ownersTable td.ownerFullName").first());
      await pause(1500);

      const cityHeader = sortHeader("City");
      await cityHeader.waitFor();
      await clickSort("City");
      await say("City is sortable too: ascending first.", cityHeader);
      await pause(1500);
      await clickSort("City");
      await say("A second click reverses it: descending.", cityHeader);
      await pause(1500);

      await page.locator("#lastName").waitFor();
      await search("D");
      await settled();
      await rangeLabel.waitFor();
      await say("Searching by last name filters on the server, and the paginator counts only the matches.", rangeLabel);
      await pause(1500);

      const matches = await get(`${apiUrl}/owners?lastName=D&page=0&size=5&sort=name,asc`).catch(() => null);
      const total = matches && (matches.totalElements ?? matches.body?.totalElements);
      if (total > 5) {
        await changeRange(() => nextButton.click());
        await say("Paging works inside the search results as well.", rangeLabel);
        await pause(1500);
      }

      await search("Zzzz");
      const none = page.locator("#noOwners");
      await none.waitFor();
      await say("A search with no match now shows a clear message instead of an empty grid.", none);
      await pause(2000);
    }
  };

  const defaultBeat = async ({route}) => {
    await page.goto(`${app}${route}`);
    const heading = page.locator("h2").first();
    await heading.waitFor();
    await say("This screen embeds a component changed in this branch.", heading);
    await pause(1500);
  };

  const {screens, changedComponents} = deriveScreens();
  const missed = [];
  const notFilmable = [];
  const visited = [];
  const filmable = screens.filter(s => {
    if (!s.route.includes(":") && !s.route.includes("*")) return true;
    notFilmable.push(`${s.route} (route parameter, no URL fills it)`);
    return false;
  });
  if (screens.length === 0)
    return {ok: false, note: `0 changed screens derived | FAILED to reach: no routed screen found for ${changedComponents.join(", ") || "any changed component"}`};

  for (const screen of filmable) {
    try {
      await (handlers[screen.route] || defaultBeat)(screen);
      visited.push(screen.route);
    } catch (e) {
      missed.push(`${screen.route} (${e.message.split("\n")[0]})`);
    }
  }

  return {
    ok: missed.length === 0,
    note: `${visited.length}/${screens.length} changed screens filmed`
      + (missed.length ? ` | FAILED to reach: ${missed.join("; ")}` : "")
      + (notFilmable.length ? ` | not filmable: ${notFilmable.join("; ")}` : "")
  };
};
