# Wiki Agents

[Русский](../README.md) · [English](README.en.md) · Español

Memoria portátil por proyecto para Claude Code y Codex. Los hooks guardan el
contexto útil en registros diarios; Haiku, Luna o Grok lo compila en una wiki
Markdown enlazada. Nunca se distribuyen memoria personal ni rutas de otra máquina.

## Inicio rápido

Envía al agente el enlace del repositorio y este mensaje:

> Instala este sistema de memoria. Lee `INSTALL.md`, hazme solo las preguntas
> necesarias, instala y prueba todo. Si algo no está disponible o falla,
> explícame el problema con palabras sencillas y dime exactamente cómo arreglarlo.

El instalador selecciona el motor principal y uno de reserva, detecta las rutas
locales, instala los hooks y prueba realmente los motores. Después, el agente
debe verificar la cadena completa `chat → hook → daily → wiki`.

Instrucciones completas: [INSTALL.md](INSTALL.es.md).
