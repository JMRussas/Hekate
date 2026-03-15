/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_GIT_HASH: string
  readonly VITE_BUILD_TIME: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
