const fs = require("fs");
const path = require("path");
const {execSync} = require("child_process");
const ts = require("typescript");

const APP_DIR = "petclinic-frontend/src/app";

const walk = d => fs.readdirSync(d, {withFileTypes: true}).flatMap(e =>
  e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]);

const baseRef = () => process.env.HUMAN_REVIEW_BASE || "eb6a0d1f";

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
  const lastButton = page.locator(".mat-mdc-paginator-navigation-last");
  const firstButton = page.locator(".mat-mdc-paginator-navigation-first");
  const pageSizeSelect = page.locator(".mat-mdc-paginator-page-size-select");
  const names = page.locator("#ownersTable td.ownerFullName");
  const sortHeader = key => page.locator(`#ownersTable th[mat-sort-header="${key}"]`);
  const isOwnersCall = r => r.request().method() === "GET" && /\/owners(\?|$)/.test(r.url());

  // run an action and wait for the owners request it triggers to be answered and rendered
  const afterReload = async action => {
    const answered = page.waitForResponse(isOwnersCall);
    await action();
    await answered;
    await pause(400);
  };
  const search = async lastName => {
    await page.locator("#lastName").fill(lastName);
    await afterReload(() => page.locator("#search-owner-form button[type='submit']").click());
  };
  const clickSort = key => afterReload(() => sortHeader(key).click());
  const pickPageSize = async size => {
    await pageSizeSelect.click();
    const option = page.locator("mat-option").filter({hasText: new RegExp(`^\\s*${size}\\s*$`)});
    await option.waitFor();
    await say("The page size is selectable: five, ten or twenty.", page.locator(".mat-mdc-select-panel"));
    await afterReload(() => option.click());
  };

  // a letter that narrows the list: it starts some of the sampled last names, not all of them
  const pickNarrowingLetter = async () => {
    try {
      const res = await get(`${apiUrl}/owners?size=20&sort=name,asc`);
      const content = (res && (res.content || res.body?.content)) || [];
      const counts = {};
      for (const o of content) {
        const l = (o.lastName || "").charAt(0).toUpperCase();
        if (l) counts[l] = (counts[l] || 0) + 1;
      }
      const letters = Object.keys(counts);
      return letters.find(l => counts[l] > 1 && counts[l] < content.length)
        || letters.find(l => counts[l] < content.length) || "D";
    } catch { return "D"; }
  };

  const handlers = {
    "/owners": async ({route}) => {
      await page.goto(`${app}${route}`);
      const table = page.locator("#ownersTable");
      await table.waitFor();
      await names.first().waitFor();
      await say("The Owners grid shows one page at a time. Ten owners to start with.", table);
      await pause(1500);

      await rangeLabel.waitFor();
      const range = (await rangeLabel.textContent()).trim();
      await say(`The paginator shows the range and the total: ${range}. The total comes from the server.`, rangeLabel);
      await pause(1500);

      await nextButton.waitFor();
      await afterReload(() => nextButton.click());
      await rangeLabel.waitFor();
      await say("Next page: the range moves on.", rangeLabel);
      await pause(1500);

      await lastButton.waitFor();
      await afterReload(() => lastButton.click());
      await firstButton.waitFor();
      await say("First and last page buttons jump to either end.", lastButton);
      await pause(1500);
      await afterReload(() => firstButton.click());

      await pageSizeSelect.waitFor();
      await afterReload(() => nextButton.click());
      await pickPageSize(5);
      await rangeLabel.waitFor();
      await say("Changing the page size goes back to the first page.", rangeLabel);
      await pause(1500);

      const nameHeader = sortHeader("name");
      await nameHeader.waitFor();
      await say("Name is the default sort, ascending.", nameHeader);
      await pause(1000);
      await clickSort("name");
      await names.first().waitFor();
      await say("Clicking Name again sorts descending.", nameHeader);
      await pause(1500);
      const sample = (await names.first().textContent()).trim();
      await say(`The first row is now ${sample}.`, names.first());
      await pause(1500);

      const cityHeader = sortHeader("city");
      await cityHeader.waitFor();
      await clickSort("city");
      await say("City is sortable too. First click: ascending.", cityHeader);
      await pause(1500);
      await clickSort("city");
      await say("Second click: descending.", cityHeader);
      await pause(1500);

      const letter = await pickNarrowingLetter();
      const lastNameInput = page.locator("#lastName");
      await lastNameInput.waitFor();
      await search(letter);
      await rangeLabel.waitFor();
      const narrowed = (await rangeLabel.textContent()).trim();
      await say(`Searching last names starting with ${letter} narrows the count to ${narrowed}.`, rangeLabel);
      await pause(1500);

      await search("Zzzz");
      const none = page.locator("#noOwners");
      await none.waitFor();
      await say("A search with no match shows a message instead of an empty grid.", none);
      await pause(2000);

      const addOwner = page.locator("#addOwner");
      await addOwner.waitFor();
      await say("Add Owner stays below the list.", addOwner);
      await pause(1500);
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
