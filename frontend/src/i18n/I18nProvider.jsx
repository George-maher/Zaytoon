/**
 * The React half of the translation layer.
 *
 * The shell (`App.jsx`) already owns the language *state* and applies it to
 * `<html lang dir>`; this provider deliberately does not duplicate either. It
 * exists so a component can say `const { t } = useI18n()` instead of threading
 * a `language` prop through four levels of layout — the prop form remains
 * supported, and both read the same table from `./strings.js`.
 *
 * Direction is exposed for components that need it (an inline-start spine, an
 * RTL-aware icon), but the provider never writes to `document`: there is one
 * owner for the document's direction and it is the shell.
 */

import { useMemo } from "react";
import { I18nContext } from "./context";
import { directionFor, translate } from "./strings";

export default function I18nProvider({ language = "en", children }) {
  const value = useMemo(
    () => ({
      language,
      dir: directionFor(language),
      t: (key, vars) => translate(language, key, vars),
    }),
    [language],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
