# Contrato de instalación para el agente

1. Clona este repositorio en una carpeta local permanente y lee
   [`../INSTALL.md`](../INSTALL.md).
2. Pide al usuario elegir Haiku, Luna o Grok y un motor de reserva opcional. Si
   no tiene otra suscripción, recomienda Haiku desde Claude y Luna desde Codex.
3. Pregunta qué proyectos usarán memoria. Nunca escanees automáticamente toda
   la carpeta personal.
4. Ejecuta `uv sync` y después `uv run python scripts/install.py` con los valores
   confirmados de `--caller`, `--engine`, `--fallback`, `--root` y `--project`.
5. Ejecuta la prueba E2E con otro chat descrita en la guía canónica rusa.

Nunca omitas silenciosamente un paso imposible o fallido. Informa `OK`,
`PARTIAL` o `FAILED`; explica qué falló, su efecto, la acción exacta para
resolverlo y qué prueba repetir. Nunca publiques memoria personal, credenciales,
rutas locales, `daily/`, `wiki/`, `.cmc/`, `ACCESS.md` ni configuración local.
