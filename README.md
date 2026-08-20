# BITVAVO LAB

Laboratorio privado para investigar ineficiencias de mercado en Bitvavo con prioridad absoluta en simulacion, trazabilidad y seguridad.

## Estado actual

Esta primera entrega implementa la Fase 1:

- infraestructura con Docker Compose;
- PostgreSQL con migraciones controladas;
- API FastAPI;
- panel privado HTML sencillo;
- descubrimiento automatico de mercados;
- snapshot inicial del libro y actualizacion por WebSocket;
- persistencia periodica razonable;
- `health` del sistema;
- `TradingGuard` central que bloquea cualquier operacion real por defecto.

## Advertencia obligatoria

EL SISTEMA NO GARANTIZA BENEFICIOS.

Una oportunidad puede desaparecer antes de ejecutarse, el precio puede moverse, una orden puede ejecutarse parcialmente, las comisiones pueden eliminar la ventaja, la latencia importa, el historico no garantiza el futuro y una simulacion puede diferir de la ejecucion real.

## Requisitos

- Docker
- Docker Compose

## Arranque

1. Copia `.env.example` a `.env`.
2. Ajusta las variables si lo necesitas.
3. Arranca:

```bash
docker compose up -d --build
```

## URLs

- Panel: `http://localhost:8080`
- API: `http://localhost:8001`
- Health: `http://localhost:8001/health`

Los puertos son configurables con `APP_PORT` y `DASHBOARD_PORT`.

## Comandos utiles

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f app
docker compose logs -f dashboard
docker compose down
```

## Copias de seguridad

Backup rapido:

```bash
docker compose exec db pg_dump -U bitvavo bitvavo_lab > backup.sql
```

Restauracion:

```bash
type backup.sql | docker compose exec -T db psql -U bitvavo -d bitvavo_lab
```

## Configuracion importante

- `MODO_OPERACION=simulacion`: modo por defecto y obligatorio al iniciar.
- `OPERACION_REAL_HABILITADA=false`: mantiene bloqueada cualquier orden real.
- `COMISION_SIMULADA_TOMADOR=0.0025`: representa 0,25 por ciento.
- `MARGEN_SEGURIDAD_PCT=0.10`: representa 0,10 por ciento, no 10 por ciento.
- `TRACK_QUOTE_CURRENCIES=EUR,USDC`: delimita las cotizaciones que seguimos en Fase 1.
- `BOOK_MARKETS_LIMIT=25`: limita el numero de libros vivos para no saturar recursos.
- `APP_PORT=8001`: puerto host para la API.
- `DASHBOARD_PORT=8080`: puerto host para el panel.

## Seguridad

- Las credenciales van en `.env`, nunca en el repositorio.
- No se registran secretos en logs.
- El ejecutor real queda bloqueado por `TradingGuard`.
- No hay retiradas ni ordenes reales en esta fase.

## Actualizacion

```bash
docker compose down
docker compose up -d --build
```

## Lo siguiente

Cuando esta base este verificada en tu maquina, el siguiente paso natural es la Fase 2: grafo de mercados, rutas triangulares y calculo bruto en simulacion.
