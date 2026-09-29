import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import babelParser from '@babel/eslint-parser'

/**
 * Flat ESLint config.
 *
 * This file was previously a Vite scaffold that could not run at all: it
 * imported four packages that were in neither `package.json` nor
 * `node_modules`, and read `reactHooks.configs.flat.recommended`, a shape
 * `eslint-plugin-react-hooks` v5 no longer exposes. There was also no `lint`
 * script, so nothing ever invoked it.
 *
 * The plugin's shared presets are spread in by hand rather than via a
 * hypothetical `extends` key: flat config has no `extends`, and writing one
 * produces a config that silently lints nothing.
 */
export default [
  {
    ignores: ['dist/**', 'node_modules/**', 'coverage/**'],
  },
  js.configs.recommended,
  reactHooks.configs['recommended-latest'],
  reactRefresh.configs.vite,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parser: babelParser,
      parserOptions: {
        ecmaVersion: 2022,
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
        requireConfigFile: false,
        babelOptions: {
          presets: ['@babel/preset-react', '@babel/preset-env'],
        },
      },
    },
    plugins: { react },
    settings: { react: { version: 'detect' } },
    rules: {
      // The base `no-unused-vars` cannot see a component that is only
      // referenced from JSX, so it flags every imported component as unused.
      // These two rules teach it about JSX without pulling in the rest of
      // eslint-plugin-react's stylistic opinions.
      'react/jsx-uses-vars': 'error',
      'react/jsx-uses-react': 'error',
      // Unused args prefixed with `_` are intentional (interface conformance).
      'no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' },
      ],
      // `catch (e) {}` is idiomatic for deliberately-ignored cleanup failures
      // (stopping a recorder, revoking an object URL). Flagging those as errors
      // only teaches people to write `catch (e) { void e }`.
      'no-empty': ['error', { allowEmptyCatch: true }],
      // `console.log` is used for voice-pipeline diagnostics; the backend logs
      // are structured, so this stays a warning rather than an error.
      'no-console': ['warn', { allow: ['warn', 'error'] }],
    },
  },
  {
    // Tests intentionally reach into globals and mock aggressively.
    files: ['**/*.test.{js,jsx}', 'src/test/**'],
    languageOptions: {
      globals: { ...globals.node, ...globals.browser },
    },
    rules: {
      'no-unused-vars': 'off',
      'no-empty': 'off',
    },
  },
]