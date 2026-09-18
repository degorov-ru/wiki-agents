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

En Codex Desktop, abre el proyecto, ejecuta `/hooks`, revisa el comando exacto y
el hash de `.codex/hooks.json`, y marca el hook como confiable. Sin este paso
manual, el hook Stop de Desktop no se ejecuta.

Usa `install.py --dry-run` para revisar el plan sin escribir ni llamar al proveedor.
Los comandos son `manage.py status|doctor|pause|resume|upgrade|rollback|uninstall|uninstall-claude`.
Upgrade crea una copia gestionada; rollback solo acepta `.cmc/backups/`; uninstall
elimina solo el hook de esta copia y conserva la memoria y los hooks ajenos.

Nunca omitas silenciosamente un paso imposible o fallido. Informa `OK`,
`PARTIAL` o `FAILED`; explica qué falló, su efecto, la acción exacta para
resolverlo y qué prueba repetir. Nunca publiques memoria personal, credenciales,
rutas locales, `daily/`, `wiki/`, `.cmc/`, `ACCESS.md` ni configuración local.
`scripts/import_source.py` solo acepta fuentes `.md` y `.txt`.

Probado: macOS, Python 3.12+, uv y Luna CLI autenticado. Linux/Windows y
Haiku/Grok requieren comprobación aparte. Extrae el archivo en una carpeta nueva
por versión; no sobrescribas una instalación con cambios pendientes. Conserva
la carpeta anterior y su configuración. Las copias de upgrade incluyen artículos.
`manage.py migrate-articles --project PATH` muestra la migración; `--apply` la
aplica. Los campos ausentes reciben hypothesis/proposed, sin reescribir texto ni
fuentes. Una fuente inválida bloquea todo el lote. Repetir produce NOOP;
`rollback --backup PATH` restaura la copia gestionada. Detén trabajo concurrente
al restaurar. La distribución pública depende de resolver LICENSING.md.
