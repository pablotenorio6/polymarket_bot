# Polymarket Trading Bot - BTC 15-Minute Markets

Bot de trading automático para mercados "Bitcoin Up or Down" de 15 minutos en Polymarket.

## 📋 Estrategia

**Concepto**: Cuando uno de los lados (UP o DOWN) alcanza un precio alto (96 centavos), compramos ESE MISMO lado esperando que continúe hasta 99+ centavos (momentum trading).

**Ejecución**:
- 🎯 **Trigger**: Cuando cualquier lado alcanza $0.96
- 💰 **Entry**: Compramos ESE MISMO lado a $0.97 (Fill or Kill)
- 🛡️ **Stop Loss**: Vendemos si el precio cae a $0.85 (protección de emergencia)
- 🎉 **Exit**: Esperamos resolución del mercado → $1.00 por acción si ganamos

**Ejemplo Ganador**:
```
1. Market: "Bitcoin Up or Down - 2:00PM-2:15PM ET"
2. UP alcanza $0.96 (momentum alcista)
3. Bot compra UP a $0.97 (costo: $9.70 por 10 acciones)
4. Market se cierra a las 2:15PM
5. Bitcoin efectivamente subió → UP gana
6. Posición se resuelve a $1.00 → Recibimos $10.00
7. Ganancia: $0.30 (3.1% ROI)
```

**Ejemplo Perdedor (Stop Loss)**:
```
1. Market: "Bitcoin Up or Down - 3:00PM-3:15PM ET"
2. DOWN alcanza $0.96
3. Bot compra DOWN a $0.97 (costo: $9.70 por 10 acciones)
4. Bitcoin sube fuertemente, DOWN colapsa a $0.85
5. Stop loss activado → Vendemos a $0.85
6. Recibimos: $8.50
7. Pérdida: $1.20 (12.4% ROI)
```

## 🚀 Instalación

### 1. Requisitos
- Python 3.8+
- Cuenta Polymarket con fondos
- Wallet private key (para firmar transacciones)

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar credenciales

**Opción A: Archivo .env (Recomendado)**

```bash
# 1. Copiar el template
cp env.example .env

# 2. Editar .env con tu private key
# Windows: notepad .env
# Linux/Mac: nano .env
```

Contenido del archivo `.env`:
```bash
POLYMARKET_PRIVATE_KEY=tu_private_key_aqui_sin_0x
```

**Opción B: Variables de entorno temporales**
```bash
# Windows PowerShell
$env:POLYMARKET_PRIVATE_KEY="tu_private_key_aqui"

# Linux/Mac
export POLYMARKET_PRIVATE_KEY="tu_private_key_aqui"
```

⚠️ **IMPORTANTE**: 
- NUNCA compartas tu private key
- El archivo `.env` está en `.gitignore` (no se subirá a git)
- Usa la key sin el prefijo `0x`

## ⚙️ Configuración

Edita `config.py` para ajustar los parámetros:

```python
# Precios de estrategia
TRIGGER_PRICE = 0.96    # Precio para activar compra
ORDER_PRICE = 0.97      # Precio de entrada
STOP_LOSS_PRICE = 0.85  # Stop loss

# Tamaño de posición
MAX_POSITION_SIZE = 10  # USD por trade

# Límites
MAX_CONCURRENT_POSITIONS = 2  # Máximo de posiciones simultáneas
```

## 🎮 Uso

### Modo Normal (Trading Activo)
```bash
python main.py
```

El bot:
1. Monitorea mercados BTC 15-min **que están ocurriendo AHORA** (no futuros)
2. Detecta oportunidades cuando un lado alcanza $0.96
3. Coloca órdenes automáticamente
4. Gestiona stop loss y take profit

⏰ **Cómo Funciona**: 
- Los mercados "Bitcoin Up or Down" de 15 minutos son parte de una serie recurrente
- El bot genera dinámicamente los slugs de eventos basándose en el timestamp actual
- Solo encuentra mercados en su ventana activa de 15 minutos
- Por ejemplo, a las 3:07 PM ET, encontrará el mercado "3:00PM-3:15PM ET"
- Los mercados se crean automáticamente cada 15 minutos

### Modo Monitor (Sin Trading)
Si no configuras `POLYMARKET_PRIVATE_KEY`, el bot corre en modo monitor:
- Muestra mercados activos
- Muestra precios en tiempo real
- NO ejecuta trades

## 📁 Estructura del Código

```
├── main.py              # Punto de entrada del bot
├── config.py            # Configuración y parámetros
├── auth.py              # Autenticación con Polymarket
├── monitor.py           # Monitoreo de mercados activos
├── trader.py            # Lógica de trading y órdenes
├── risk_manager.py      # Stop loss y gestión de riesgo
├── requirements.txt     # Dependencias
└── README.md           # Esta documentación
```

## 🔧 Componentes

### `monitor.py` - Monitoreo de Mercados
- Busca mercados "Bitcoin Up or Down" de 15 minutos activos
- Obtiene precios de CLOB `/midpoint` endpoint (precios en tiempo real)
- Acceso al order book completo para información de trading

**Precios utilizados**:
- **CLOB Midpoint** ($0.18/$0.82): Precio real de mercado para monitoreo
- **Order Book** ($0.01/$0.99): Spreads enormes, no útil para monitoreo
- **outcomePrices** ($0.49/$0.51): Última transacción, puede estar desactualizada

El bot usa CLOB midpoint para detectar oportunidades ($0.96 trigger) y luego coloca órdenes Fill-or-Kill a precio específico ($0.97).

### `trader.py` - Trading
- Coloca órdenes Fill or Kill
- Rastrea posiciones activas
- Calcula P&L

### `risk_manager.py` - Gestión de Riesgo
- Stop loss automático
- Take profit automático
- Límites de posiciones concurrentes

### `auth.py` - Autenticación
- Maneja autenticación con Polymarket
- Usa `py-clob-client` para firmar transacciones

## 📊 Logs

El bot genera logs en:
- **Consola**: Output en tiempo real
- **Archivo**: `trading_bot.log`

Niveles de log configurables en `config.py`:
- `DEBUG`: Información detallada
- `INFO`: Eventos importantes (default)
- `WARNING`: Advertencias
- `ERROR`: Errores

## ⚠️ Riesgos y Consideraciones

### Riesgos Financieros
- **Pérdidas**: Puedes perder dinero. Usa solo capital que puedas permitirte perder
- **Slippage**: Órdenes Fill or Kill pueden no ejecutarse si no hay liquidez
- **Gas fees**: Transacciones en Polygon tienen comisiones

### Limitaciones Técnicas
- **Granularidad**: Los datos históricos de precios tienen resolución ~10 min
- **Latencia**: Polling cada 2 segundos puede perder spikes rápidos
- **Liquidez**: Mercados pequeños pueden tener poca liquidez

### Recomendaciones
1. **Empieza pequeño**: Usa `MAX_POSITION_SIZE = 1` para pruebas
2. **Monitorea**: Revisa los logs frecuentemente
3. **Ajusta stop loss**: Encuentra el balance entre protección y volatilidad
4. **Diversifica**: No pongas todo en un solo mercado

## 🐛 Troubleshooting

### Error: "POLYMARKET_PRIVATE_KEY not set"
- Configura la variable de entorno con tu private key

### Error: "py-clob-client not installed"
```bash
pip install py-clob-client
```

### Bot no encuentra mercados
- Verifica que haya mercados BTC 15-min activos en Polymarket
- Los mercados solo están activos en horarios específicos

### Órdenes no se ejecutan
- Verifica que tienes fondos suficientes
- Las órdenes Fill or Kill requieren liquidez inmediata
- Ajusta `ORDER_PRICE` si es necesario

## 📚 Recursos

- [Polymarket Docs](https://docs.polymarket.com/)
- [CLOB API Docs](https://docs.polymarket.com/#clob-api)
- [py-clob-client](https://github.com/Polymarket/py-clob-client)

## 🔒 Seguridad

- ✅ Usa variables de entorno para credenciales
- ✅ NUNCA hagas commit de tu private key
- ✅ Agrega `.env` a `.gitignore`
- ✅ Usa wallets dedicadas para trading bots

## 📝 Notas

- El código anterior (análisis histórico) está en `/overbetted_test`
- Este bot opera en tiempo real, no analiza datos históricos
- Probado con mercados BTC 15-min en Polymarket Polygon

## ⚖️ Disclaimer

Este software se proporciona "tal cual" sin garantías. El trading automatizado conlleva riesgos significativos. El autor no se hace responsable de pérdidas financieras.

**Úsalo bajo tu propio riesgo.**

