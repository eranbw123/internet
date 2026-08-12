// elkjs ships its browser-bundle entry (elk.bundled.js) as plain JS with no
// co-located declaration file for that specific subpath (only its main
// "elkjs" entry is typed) -- without this ambient shim, `tsc -b` errors on
// `import("elkjs/lib/elk.bundled.js")` in graph/elkLayout.ts ("could not
// find a declaration file"). Declared as `any` deliberately: elkLayout.ts
// immediately narrows the import to this repo's own minimal `ElkLike`
// interface and never relies on elkjs's real type surface, so an untyped
// shim here costs nothing.
declare module "elkjs/lib/elk.bundled.js" {
  const ELK: any;
  export default ELK;
}
