<p align="center">
  <img src="../assets/logo.svg" alt="Logo de keepr" width="160">
</p>

<p align="center">
  <strong>Un asistente RAG de documentos local y centrado en la privacidad — tus documentos, tu máquina, tus respuestas.</strong>
</p>

<p align="center">
  <a href="../README.md">English</a> •
  <a href="README.es.md">Spanish</a> •
  <a href="README.zh.md">简体中文</a> •
  <a href="README.ru.md">Русский</a> •
  <a href="README.fr.md">Français</a>
</p>

<p align="center">
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/github/stars/pironc/keepr?style=social" alt="GitHub stars" data-canonical-src="https://img.shields.io/github/stars/pironc/keepr?style=social" style="max-width: 100%;"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="Licencia"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/badge/100%25-Local-FF6600" alt="100% Local"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/github/issues/pironc/keepr?style=flat-square&color=blue" alt="Issues"></a>
</p>

<p align="center">
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-mac-aarch64.dmg"><img src="https://img.shields.io/badge/OSX_ARM-FF6600?logo=apple&logoColor=white" alt="macOS Apple Silicon"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-mac-x86_64.dmg"><img src="https://img.shields.io/badge/OSX_x86-FF6600?logo=apple&logoColor=white" alt="macOS Intel"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-linux-x86_64.AppImage"><img src="https://img.shields.io/badge/Linux-FF6600?logo=linux&logoColor=white" alt="Linux"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-windows-x86_64-setup.exe"><img src="https://img.shields.io/badge/Windows-FF6600?logo=data:image/svg%2Bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA4OCA4OCIgZmlsbD0iI2ZmZmZmZiI+CiAgPHBhdGggZD0iTTAgMTIuNDAybDM1LjY4Ny00Ljg2LjAxNiAzNC40MjMtMzUuNjcuMjAzeiIvPgogIDxwYXRoIGQ9Ik0zNS42NyA0NS45MzFsLjAyOCAzNC40NTNMLjAyOCA3NS40OC4wMjYgNDUuN3oiLz4KICA8cGF0aCBkPSJNNDAuMDAyIDcuMzc3TDg3LjMxNCAwdjQxLjUyN2wtNDcuMzE4LjM3NnoiLz4KICA8cGF0aCBkPSJNODcuMzMxIDQ1LjkwNmwtLjAxMSA0MS4zNC00Ny4zMTgtNi42NzgtLjA2Ni0zNC43Mzl6Ii8+Cjwvc3ZnPgo=" alt="Windows"></a>
</p>

# keepr

**keepr** es una aplicación de chat con generación aumentada por recuperación (RAG) que se ejecuta completamente en tu propia máquina. Sube archivos PDF o de texto, haz preguntas y obtén respuestas basadas exclusivamente —y solo— en lo que has subido, con citas en línea que apuntan al pasaje exacto de origen. Sin API en la nube, sin que tus datos salgan de tu dispositivo, completamente funcional con la red desconectada una vez descargados los modelos.

<p align="center">
  <img src="../assets/keepr.jpg" alt="keepr — asistente RAG de documentos centrado en la privacidad" width="820">
</p>

## Índice

- [¿Por qué keepr?](#por-qué-keepr)
- [Funcionalidades](#funcionalidades)
- [Primeros pasos](#primeros-pasos)
  - [Docker](#docker)
  - [Desarrollo local](#desarrollo-local)
  - [Variables de entorno](#variables-de-entorno)
  - [Ejecución con modelos locales reales](#ejecución-con-modelos-locales-reales)
  - [Aplicación de escritorio nativa (macOS)](#aplicación-de-escritorio-nativa-macos)
- [Desarrollo](#desarrollo)
  - [Estructura del proyecto](#estructura-del-proyecto)
  - [Comandos](#comandos)
  - [Pruebas](#pruebas)
  - [Solución de problemas](#solución-de-problemas)
- [Licencia](#licencia)

Para el fundamento tecnológico de cada decisión, el diseño completo del sistema, el formato de comunicación y la historia de escalado, consulta **[ARCHITECTURE.md](../ARCHITECTURE.md)** (en inglés).

## ¿Por qué keepr?

La mayoría de las herramientas RAG envían tus documentos a una API en la nube (riesgo de privacidad) o envuelven un servidor local opaco como Ollama (funcionamiento interno inaccesible). keepr toma un tercer camino: **cada capa está construida a mano, documentada y te pertenece** — desde las matemáticas del índice vectorial hasta la transmisión de tokens del LLM. La tesis es que un sistema RAG a escala personal no necesita infraestructura distribuida; necesita código claro y correcto que puedas leer y entender de principio a fin.

- **Tus datos nunca salen de tu máquina.** Sin telemetría, sin claves de API en la nube, sin llamadas de red durante la inferencia.
- **Cada decisión tecnológica tiene una razón documentada y específica** — no «porque es popular». Consulta [ARCHITECTURE.md](../ARCHITECTURE.md) para conocer el fundamento completo de cada decisión.
- **El mecanismo anti-alucinación es determinista** (una comparación de floats contra un umbral de similitud), no un prompt pidiéndole a un modelo de 7-8B que se autocontrole.
- **Diseñado para escalar** desde un portátil a un servidor dedicado sin reescribir nada — cada capa está detrás de una interfaz con nombre que hoy tiene exactamente una implementación concreta, lista para una segunda cuando se necesite.

## Funcionalidades

### 🔒 Privacidad y funcionamiento sin conexión
- **Completamente local por defecto.** `LLM_DRIVER=mock` / `EMBEDDER=mock` (los valores predeterminados) ejecutan todo el proceso de ingesta → recuperación → citas sin necesidad de descargar modelos — comprobable en milisegundos.
- **Modelos locales reales** mediante `llama-cpp-python` (formato GGUF) en Metal (Apple Silicon), CUDA o CPU.
- **Aislado de la red por diseño.** El frontend está escrito a mano en HTML/CSS/JS vanilla sin dependencias externas — sin etiquetas script de CDN, sin paso de compilación npm. Las fuentes tipográficas están almacenadas localmente como archivos `.woff2`; los iconos están creados a mano / almacenados localmente como archivos `.svg` (sin CDN de iconos).
- **Aislamiento de red comprobado.** Toda la suite de pruebas se ejecuta con `pytest-socket` bloqueando todo acceso real a la red, incluyendo una prueba de control positivo que demuestra que el bloqueo está realmente activo.

### 📄 Chat RAG con anti-alucinación determinista
- Cada respuesta está respaldada por fragmentos recuperados de los documentos *de esa conversación* — no de un índice global ni de los datos de preentrenamiento del modelo.
- **Rechazo previo al LLM:** si ningún fragmento recuperado supera el umbral de similitud coseno, keepr rechaza la consulta *antes* de llamar al LLM — una comparación determinista de `float`, no un juicio delegado al modelo.
- **Verificación de citas post-generación:** después de que el LLM transmite su respuesta, cada etiqueta de cita `[chunk_N]` se verifica contra el conjunto de IDs de fragmentos realmente recuperados en ese turno. El modelo no puede inventar una cita a un documento que nunca tuvo delante — esto se aplica por pertenencia a conjunto, no confiando en la salida del modelo.
- **Umbral individual por fragmento:** cada fragmento recuperado se compara contra el umbral de similitud individualmente, no solo el mejor — una única coincidencia fuerte no puede arrastrar varios fragmentos apenas relacionados al contexto.

### 🗂️ Alcance de recuperación por conversación
- Cada conversación tiene su propio alcance de recuperación, como un Proyecto de Claude o un cuaderno de NotebookLM.
- La barra lateral te permite alternar entre conversaciones, buscar por título, fijar favoritas y renombrar o eliminar directamente mediante un menú contextual con clic derecho.
- Los documentos se deduplican por hash de contenido (SHA-256) — volver a adjuntar el mismo archivo es una operación nula, no una duplicación silenciosa de cada fragmento.

### ⚡ Pipeline de ingesta en vivo
- Un archivo se prepara al soltarlo y se procesa en el momento en que pulsas enviar: **extraer → fragmentar → incrustar → indexar**.
- Una animación de estado por archivo (`staged → extracting → chunking → embedding → indexed`) se controla mediante eventos SSE reales del backend, no por un temporizador falso.
- Cada tipo de archivo pasa por el mismo pipeline — el protocolo `Ingestor` es lo que hace que el soporte de audio/vídeo sea ampliable más adelante sin tocar nada posterior en la cadena.
- Se ejecuta como su propia tarea en segundo plano (`IngestionWorker`), independiente de tu conexión HTTP — cerrar la pestaña o perder la red a mitad de una subida no pausa ni pierde el trabajo de extraer→fragmentar→incrustar→indexar en curso; volver a abrir la conversación se reconecta a él en vivo.

### 🔄 Generación duradera e independiente de la conexión
- En el momento en que pulsas enviar, la generación del mensaje se ejecuta como una **tarea en segundo plano** (`GenerationWorker`) independiente de tu conexión HTTP, en su propia cola separada de la ingesta — una incrustación solo espera a una incrustación anterior, nunca a una generación de LLM no relacionada.
- Recarga la página a mitad de una respuesta — la respuesta sigue generándose y se guarda en la base de datos de todos modos. Al recargar, te reconectas a ella en vivo, esté donde esté.
- Recuperación ante fallos: al iniciar, cualquier mensaje o documento atascado a mitad de proceso por una muerte anterior del proceso se marca como erróneo con su contenido parcial preservado, en lugar de quedarse girando para siempre.

### ⚙️ Gestión de modelos integrada
- El menú de Ajustes lista cada `.gguf` en `models/`, clasificado como modelo de LLM o de incrustación a partir de sus propios metadatos (una clave de capa de pooling), nunca por el nombre del archivo — sin lista de nombres de modelo codificada que mantener al día conforme aparecen nuevas arquitecturas.
- Descarga un modelo directamente desde Ajustes (Hugging Face Hub, con progreso en vivo) en lugar de ejecutar primero un paso de CLI aparte.
- Cambiar el modelo activo de LLM o de incrustación guarda la elección y reinicia la app para que el nuevo modelo se cargue de verdad; eliminar un archivo de modelo está disponible de la misma forma.

### 🧱 Arquitectura conectable
Cada capa está detrás de un protocolo/ABC — intercambia implementaciones sin reestructurar:

| Capa | Interfaz | Implementaciones |
|---|---|---|
| **Driver LLM** | `LLMDriver` | `MockLLMDriver` (determinista, sin descarga), `LlamaCppDriver` (inferencia GGUF real) |
| **Incrustador** | `Embedder` | `MockEmbedder` (truco de hash tipo bolsa de palabras), `LlamaCppEmbedder` (nomic-embed-text-v2-moe, multilingüe) |
| **Índice vectorial** | `VectorIndex` | `NumpyFlatIndex` (float32, exacto), `QuantizedNumpyFlatIndex` (cuantización escalar int8, ~4× menos memoria) |
| **Ingestor** | `Ingestor` | `PdfIngestor`, `TextIngestor`, `AudioVideoIngestor` (stub limpio — lanza `UnsupportedSourceError`) |
| **Almacenamiento** | `Repository` | SQLite mediante `aiosqlite` (el único módulo que habla SQL — cambiar a Postgres es un cambio de un solo archivo) |

### 🖥️ Aplicación de escritorio nativa
- Un wrapper de [Tauri](https://tauri.app/) v2 empaqueta el backend Python (compilado con PyInstaller) en un bundle nativo `.app` de macOS y un instalador `.dmg`.
- No se requiere ninguna instalación de Python en la máquina de destino — el backend es un binario independiente incrustado dentro de la aplicación.
- El mismo código se ejecuta como aplicación web (`make run` para desarrollo con recarga en caliente) o como aplicación de escritorio nativa (`make tauri-build` para distribución).

---

## Primeros pasos

### Docker

La forma más rápida de tener una instancia en funcionamiento — sin necesidad de configurar Python:

**Requisitos previos:** [Docker](https://docs.docker.com/get-docker/) con Compose v2.

```bash
git clone https://github.com/pironc/keepr.git
cd keepr
docker compose up --build
```

Abre [http://localhost:8000](http://localhost:8000). Suelta un PDF o un archivo `.txt` en el chat y haz una pregunta. El archivo Compose usa por defecto `LLM_DRIVER=mock` / `EMBEDDER=mock` — todo el pipeline se ejecuta sin necesidad de descargar modelos.

Para inferencia local real, monta tus modelos y cambia los drivers:

```bash
# Primero, descarga los modelos GGUF en models/ (solo una vez):
python scripts/download_models.py

# Luego sobrescribe las variables de entorno de Compose:
LLM_DRIVER=llama_cpp EMBEDDER=llama_cpp docker compose up --build
```

### Desarrollo local

Ejecuta de forma nativa con recarga en caliente — sin necesidad de Docker:

**Requisitos previos:** Python 3.12+.

```bash
git clone https://github.com/pironc/keepr.git
cd keepr

python3 -m venv .venv && source .venv/bin/activate
make install
make run
```

Abre [http://localhost:8000](http://localhost:8000). Los valores predeterminados (`LLM_DRIVER=mock`, `EMBEDDER=mock`) no requieren ninguna descarga — todo el pipeline de ingesta → recuperación → citas se puede probar de inmediato.

### Variables de entorno

Copia `.env.example` a `.env` para personalizarlas. Las esenciales:

| Variable | Propósito |
|---|---|
| `LLM_DRIVER` | `mock` o `llama_cpp`. Déjalo sin definir para autodetectar: `llama_cpp` en cuanto exista realmente un GGUF (p. ej. descargado/seleccionado en Ajustes), `mock` en caso contrario — sin descargas en una instalación nueva, sin necesitar la variable una vez que existe un modelo. |
| `LLM_MODEL_PATH` | Ruta al archivo GGUF. Solo cuando `LLM_DRIVER=llama_cpp`. Déjalo sin configurar para usar el modelo elegido en el menú de Ajustes. |
| `LLM_CONTEXT_WINDOW` | Tamaño de la ventana de contexto (por defecto se detecta automáticamente desde `MEMORY_TIER`, típicamente `8192`). |
| `LLM_GPU_LAYERS` | Capas a descargar en la GPU (`-1` = todas, por defecto). |
| `EMBEDDER` | `mock` o `llama_cpp`. Misma autodetección que `LLM_DRIVER`, evaluada de forma independiente. |
| `EMBEDDING_MODEL_PATH` | Ruta al modelo GGUF de incrustación. Solo cuando `EMBEDDER=llama_cpp`. Déjalo sin configurar para usar el modelo elegido en el menú de Ajustes. |
| `EMBEDDING_GPU_LAYERS` | Capas de GPU para incrustación (por defecto `0` — la incrustación se ejecuta en CPU para evitar contención de GPU con el LLM). |
| `VECTOR_INDEX_BACKEND` | `flat` (float32, exacto) o `quantized` (cuantización escalar int8, ~4× menos memoria). |
| `RETRIEVAL_TOP_K` | Número de fragmentos a recuperar por consulta (por defecto `5`). |
| `RETRIEVAL_MIN_SIMILARITY` | Umbral de similitud coseno por debajo del cual el motor se niega a responder (por defecto `0.22` — calibrado para nomic-embed-text-v2-moe). |
| `MODELS_DIR` | Directorio donde se buscan los archivos de modelo `.gguf` (por defecto `models`). |
| `DATABASE_PATH` | Ruta de la base de datos SQLite (por defecto `data/keepr.db`). |
| `INDEX_DIR` | Directorio de índices vectoriales por conversación (por defecto `data/index`). |
| `UPLOAD_DIR` | Almacenamiento de archivos subidos (por defecto `data/uploads`). |
| `MEMORY_TIER` | Sobrescribe el presupuesto de memoria auto-detectado (`minimal`, `standard`, `large`, `server`). Déjalo sin configurar para detección automática. |
| `CHUNK_SIZE` | Caracteres por fragmento (por defecto `800`). |
| `CHUNK_OVERLAP` | Solapamiento entre fragmentos consecutivos (por defecto `150`). |
| `LOG_LEVEL` | Nivel de registro (por defecto `INFO`). |

### Ejecución con modelos locales reales

Instala el driver `llama_cpp` — un extra aparte, más pesado (compila `llama-cpp-python` desde el código fuente; en Apple Silicon activa la aceleración Metal automáticamente, sin flags adicionales):

```bash
pip install -e ".[llama]"
```

Luego descarga el par Qwen3-8B + nomic-embed-text-v2-moe GGUF (~7.5 GB en total) desde el menú de **Ajustes** de la app, o desde la CLI:

```bash
python scripts/download_models.py
```

Luego en `.env`:

```env
LLM_DRIVER=llama_cpp
EMBEDDER=llama_cpp
```

Reinicia `make run`. El primer mensaje será más lento (carga del modelo en memoria); todo lo posterior se ejecuta desde el stack residente ya caliente.

Para usar un modelo distinto, coloca cualquier GGUF compatible con llama.cpp en `models/` y elígelo en el menú de **Ajustes** — la elección se guarda y se aplica en el próximo reinicio. También puedes definir `LLM_MODEL_PATH` / `EMBEDDING_MODEL_PATH` directamente para anularlo. (El modelo de incrustación no es tan libre de cambiar: `RETRIEVAL_MIN_SIMILARITY` está calibrado para nomic-embed-text-v2-moe, así que cambiarlo implica recalibrar ese umbral.)

### Aplicación de escritorio nativa (macOS)

```bash
# Modo desarrollo — abre una ventana nativa apuntando a http://localhost:8000.
# El backend Python ya debe estar en ejecución (`make run` en otra terminal).
make tauri-dev

# Compilación de producción — compila el backend Python a un binario independiente,
# lo empaqueta en un .app y genera un instalador .dmg.
make tauri-build
```

El `.app` compilado no requiere instalación de Python en la máquina de destino — el backend es un binario PyInstaller autónomo incrustado dentro del bundle.

---

## Desarrollo

### Estructura del proyecto

```
keepr/
├── src/
│   ├── models.py              # Modelos Pydantic v2 — fuente única de verdad
│   ├── config.py              # Detección de hardware, niveles de memoria, Settings
│   ├── concurrency.py         # Wrappers LockedEmbedder / LockedLLMDriver
│   ├── logger.py              # Registro estructurado
│   ├── download.py            # Helpers de descarga de modelos (Hugging Face Hub, compartidos con la CLI)
│   ├── gguf_meta.py           # Clasificador ligero de metadatos GGUF (detección de pooling)
│   ├── model_unavailable.py   # ModelUnavailableError — archivo GGUF ausente/roto, por rol
│   ├── api/
│   │   ├── app.py             # Fábrica de la app FastAPI + lifespan
│   │   ├── context.py         # AppContext + DI
│   │   ├── routes_conversations.py
│   │   ├── routes_messages.py # Endpoint SSE multiplexado
│   │   ├── routes_models.py   # Endpoints de estado/selección/descarga de modelos
│   │   └── sse.py             # Helpers de formato SSE
│   ├── db/
│   │   ├── pool.py            # SQLiteConnectionPool
│   │   ├── repository.py      # El ÚNICO módulo que habla SQL
│   │   └── schema.py          # Sentencias CREATE TABLE
│   ├── embeddings/
│   │   ├── base.py            # Protocolo Embedder
│   │   ├── mock_embedder.py   # Incrustador determinista por truco de hash
│   │   ├── llama_cpp_embedder.py  # Incrustación GGUF real
│   │   └── factory.py
│   ├── ingestion/
│   │   ├── base.py            # Protocolo Ingestor
│   │   ├── pipeline.py        # Orquesta extraer→fragmentar→incrustar→indexar
│   │   ├── worker.py          # Tarea en segundo plano — duradera, independiente de la conexión
│   │   ├── chunker.py         # Fragmentación de texto con solapamiento
│   │   ├── registry.py        # Encuentra el ingestor adecuado para un archivo
│   │   ├── pdf_ingestor.py
│   │   ├── text_ingestor.py
│   │   └── audio_video_ingestor.py  # Stub limpio — lanza UnsupportedSourceError
│   ├── llm/
│   │   ├── base.py            # LLMDriver ABC (stream asíncrono de tokens)
│   │   ├── mock_driver.py     # Mock determinista para pruebas
│   │   ├── llama_cpp_driver.py    # Inferencia GGUF real con filtro de thinking
│   │   └── factory.py
│   ├── rag/
│   │   ├── engine.py          # RAG principal: recuperación + umbral + generación + verificación de citas
│   │   ├── prompts.py         # System prompt de fundamentación
│   │   ├── generation_worker.py   # Tarea en segundo plano — duradera, independiente de la conexión
│   │   ├── index_manager.py   # Caché de índices vectoriales por conversación
│   │   ├── greeting.py        # Detección rápida de saludo/despedida
│   │   └── title.py           # Títulos de conversación generados por LLM
│   ├── vectorstore/
│   │   ├── base.py            # Protocolo VectorIndex
│   │   ├── flat_index.py      # NumpyFlatIndex (float32, exacto)
│   │   ├── quantized_flat_index.py  # Cuantización escalar int8
│   │   ├── similarity.py      # Normalización L2
│   │   └── factory.py
│   └── web/                   # HTML/CSS/JS vanilla — cero dependencias externas
│       ├── templates/index.html
│       └── static/
│           ├── app.js
│           ├── app.css
│           └── fonts/         # Archivos .woff2 locales (sin CDN de Google Fonts)
├── src-tauri/                 # Wrapper de escritorio nativo Tauri v2 (Rust)
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── src/lib.rs
│   └── binaries/              # Binario del backend compilado con PyInstaller
├── tests/                     # pytest + pytest-asyncio + pytest-socket
├── models/                    # Pesos de modelos .gguf descargados (MODELS_DIR)
├── scripts/
│   └── download_models.py     # Descargador único de modelos GGUF (script de red exento de tests)
├── assets/                    # Logo, capturas de pantalla
│   └── icons/                 # Iconos de marca blancos para los botones de descarga
├── ARCHITECTURE.md            # Diseño técnico completo e historia de escalado
├── CLAUDE.md                  # Guía para asistentes AI / contribuidores
├── Makefile
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

### Comandos

```bash
make install          # pip install -e ".[dev]"
make run              # uvicorn src.api.app:app --reload --port 8000
make test             # pytest
make lint             # ruff check .
make typecheck        # mypy --strict
make ci               # lint + typecheck + test — ejecutar antes de dar nada por terminado
make clean            # borra DB/índice/subidas (estado de la app) — NO los pesos de los modelos
make clean-models     # borra también models/
make wipe             # reinicio completo a un estado recién clonado (venv, cachés,
                       # salida de compilación, src-tauri/target, etc.) — models/ sigue exento
```

### Pruebas

```bash
make ci   # ruff check . && mypy && pytest
```

La suite de pruebas se ejecuta con el acceso a la red deshabilitado globalmente (`pytest-socket`) — cualquier prueba que abra un socket de red real hace fallar toda la suite. Los drivers e incrustadores mock hacen que las pruebas sean rápidas y deterministas (sin descargas de modelos, sin llamadas API inestables). `asyncio_mode = "auto"` significa que `async def test_...` simplemente funciona.

Archivos de prueba clave:
- `tests/test_rag_engine.py` — rechazo por umbral, verificación de citas (incluyendo un driver que deliberadamente inventa una cita)
- `tests/test_generation_worker.py` — generación independiente de la conexión, recuperación ante fallos, reconexión del observador
- `tests/test_ingestion_worker.py` — ingesta independiente de la conexión, recuperación ante fallos, temporización de descarga por inactividad
- `tests/test_ingestion_pipeline.py` — deduplicación por hash de contenido, transiciones de estado
- `tests/test_vectorstore.py` — benchmarks de ratio de memoria de cuantización y concordancia top-1
- `tests/test_airgapped.py` — control positivo que demuestra que el bloqueo de sockets está activo
- `tests/test_llama_cpp_driver.py` — filtro de thinking a través de etiquetas divididas, sin necesidad de modelo

### Solución de problemas

**Las respuestas parecen no estar relacionadas con mis documentos**

La similitud de recuperación podría estar por debajo del umbral — revisa los registros del servidor para ver la puntuación real de similitud coseno y compárala con `RETRIEVAL_MIN_SIMILARITY`. Si estás usando un incrustador diferente a nomic-embed-text-v2-moe, recalibra el umbral (consulta `ARCHITECTURE.md`).

**El driver mock da respuestas genéricas**

El driver mock analiza las etiquetas `[chunk_N]` del system prompt para obtener respuestas deterministas. Si no se recuperaron fragmentos (índice vacío), rechazará la consulta. Añade un documento primero.

**`make run` falla con un error de importación**

Ejecuta `make install` primero — el paquete necesita instalarse en modo editable.

---

## Licencia

keepr está bajo la [Licencia MIT](../LICENSE).

🤖 Generado con [Claude Code](https://claude.com/claude-code)
