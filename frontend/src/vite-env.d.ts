/// <reference types="vite/client" />

/** Injected by Vite's `define` from package.json — see vite.config.ts. */
declare const __APP_VERSION__: string

declare module '*.css' {
  const content: string
  export default content
}
