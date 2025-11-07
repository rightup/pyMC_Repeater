# Stats Data Collection & Charting Examples

This document provides examples for using the pyMC_Repeater API endpoints to create charts and visualizations for network monitoring.

## Available API Endpoints

### Basic Statistics
- `/api/packet_stats` - Get packet statistics for a time period
- `/api/recent_packets` - Get recent packets with all fields
- `/api/filtered_packets` - Get packets with filtering options
- `/api/packet_by_hash` - Get specific packet by hash

### Time Series Data for Charts
- `/api/packet_type_graph_data` - Get packet type data for graphing
- `/api/metrics_graph_data` - Get metrics data for graphing
- `/api/packet_type_stats` - Get packet type distribution
- `/api/rrd_data` - Get raw RRD time series data

### Noise Floor Monitoring
- `/api/noise_floor_history` - Get noise floor history (SQLite data)
- `/api/noise_floor_stats` - Get noise floor statistics
- `/api/noise_floor_chart_data` - Get noise floor RRD chart data

## Noise Floor API Examples

### Fetch Noise Floor History
```javascript
// Get last 24 hours of noise floor data from SQLite
async function fetchNoiseFloorHistory() {
  const response = await fetch('/api/noise_floor_history?hours=24');
  const result = await response.json();
  
  if (result.success) {
    const history = result.data.history;
    console.log(`Found ${history.length} noise floor measurements`);
    
    // Each record: { timestamp: 1234567890.123, noise_floor_dbm: -95.5 }
    history.forEach(record => {
      console.log(`${new Date(record.timestamp * 1000).toISOString()}: ${record.noise_floor_dbm} dBm`);
    });
  } else {
    console.error('Error:', result.error);
  }
}
```

### Fetch Noise Floor Statistics
```javascript
// Get statistical summary of noise floor data
async function fetchNoiseFloorStats() {
  const response = await fetch('/api/noise_floor_stats?hours=24');
  const result = await response.json();
  
  if (result.success) {
    const stats = result.data.stats;
    console.log('Noise Floor Statistics:');
    console.log(`Count: ${stats.count}`);
    console.log(`Average: ${stats.average?.toFixed(1)} dBm`);
    console.log(`Min: ${stats.min} dBm`);
    console.log(`Max: ${stats.max} dBm`);
    console.log(`Std Dev: ${stats.std_dev?.toFixed(2)}`);
  }
}
```

### Fetch Chart-Ready Noise Floor Data
```javascript
// Get RRD-based noise floor data optimized for charts
async function fetchNoiseFloorChartData() {
  const response = await fetch('/api/noise_floor_chart_data?hours=24');
  const result = await response.json();
  
  if (result.success) {
    const chartData = result.data.chart_data;
    
    // Data points: [[timestamp_ms, noise_floor_dbm], ...]
    chartData.data_points.forEach(point => {
      const [timestamp_ms, value] = point;
      console.log(`${new Date(timestamp_ms).toISOString()}: ${value} dBm`);
    });
    
    console.log('Statistics:', chartData.statistics);
  }
}
```

## Noise Floor Chart Examples

### 1. Noise Floor Time Series (Chart.js)

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
</head>
<body>
    <canvas id="noiseFloorChart" width="800" height="400"></canvas>
    
    <script>
        async function createNoiseFloorChart() {
            const response = await fetch('/api/noise_floor_chart_data?hours=24');
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            const chartData = result.data.chart_data;
            const stats = chartData.statistics;
            
            const ctx = document.getElementById('noiseFloorChart').getContext('2d');
            new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: 'Noise Floor',
                        data: chartData.data_points,
                        borderColor: '#FF6384',
                        backgroundColor: '#FF638420',
                        fill: true,
                        tension: 0.1,
                        pointRadius: 1,
                        pointHoverRadius: 5
                    }, {
                        label: `Average (${stats.average.toFixed(1)} dBm)`,
                        data: chartData.data_points.map(point => [point[0], stats.average]),
                        borderColor: '#36A2EB',
                        borderDash: [5, 5],
                        pointRadius: 0,
                        fill: false
                    }]
                },
                options: {
                    responsive: true,
                    plugins: {
                        title: {
                            display: true,
                            text: 'Noise Floor Over Time (Last 24 Hours)'
                        },
                        subtitle: {
                            display: true,
                            text: `Min: ${stats.min} dBm | Max: ${stats.max} dBm | Std Dev: ${stats.std_dev.toFixed(2)}`
                        }
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                displayFormats: {
                                    hour: 'MMM dd HH:mm'
                                }
                            },
                            title: {
                                display: true,
                                text: 'Time'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'Noise Floor (dBm)'
                            },
                            min: Math.min(stats.min - 5, -120),
                            max: Math.max(stats.max + 5, -60)
                        }
                    }
                }
            });
        }
        
        createNoiseFloorChart();
    </script>
</body>
</html>
```

### 2. Noise Floor Distribution Histogram

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <canvas id="noiseDistributionChart" width="600" height="400"></canvas>
    
    <script>
        async function createNoiseDistributionChart() {
            const response = await fetch('/api/noise_floor_history?hours=168'); // 1 week
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            const history = result.data.history;
            const values = history.map(record => record.noise_floor_dbm);
            
            // Create histogram bins
            const min = Math.min(...values);
            const max = Math.max(...values);
            const binCount = 20;
            const binSize = (max - min) / binCount;
            
            const bins = Array(binCount).fill(0);
            const binLabels = [];
            
            for (let i = 0; i < binCount; i++) {
                const binStart = min + (i * binSize);
                const binEnd = binStart + binSize;
                binLabels.push(`${binStart.toFixed(1)} to ${binEnd.toFixed(1)}`);
                
                values.forEach(value => {
                    if (value >= binStart && value < binEnd) {
                        bins[i]++;
                    }
                });
            }
            
            const ctx = document.getElementById('noiseDistributionChart').getContext('2d');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: binLabels,
                    datasets: [{
                        label: 'Frequency',
                        data: bins,
                        backgroundColor: '#4BC0C0',
                        borderColor: '#36A2EB',
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    plugins: {
                        title: {
                            display: true,
                            text: 'Noise Floor Distribution (Last Week)'
                        }
                    },
                    scales: {
                        x: {
                            title: {
                                display: true,
                                text: 'Noise Floor Range (dBm)'
                            }
                        },
                        y: {
                            beginAtZero: true,
                            title: {
                                display: true,
                                text: 'Count'
                            }
                        }
                    }
                }
            });
        }
        
        createNoiseDistributionChart();
    </script>
</body>
</html>
```

### 3. Real-time Noise Floor Monitor

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        .noise-monitor { padding: 20px; }
        .current-stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat-card { padding: 15px; background: #f8f9fa; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 24px; font-weight: bold; }
        .stat-label { font-size: 14px; color: #666; }
    </style>
</head>
<body>
    <div class="noise-monitor">
        <h2>Real-time Noise Floor Monitor</h2>
        
        <div class="current-stats">
            <div class="stat-card">
                <div class="stat-value" id="currentNoise">-- dBm</div>
                <div class="stat-label">Current</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="avgNoise">-- dBm</div>
                <div class="stat-label">1h Average</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="minNoise">-- dBm</div>
                <div class="stat-label">1h Min</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="maxNoise">-- dBm</div>
                <div class="stat-label">1h Max</div>
            </div>
        </div>
        
        <canvas id="realTimeChart" width="800" height="400"></canvas>
    </div>
    
    <script>
        let chart = null;
        let lastUpdateTime = 0;
        
        async function updateNoiseFloorData() {
            try {
                // Get chart data for the last hour
                const chartResponse = await fetch('/api/noise_floor_chart_data?hours=1');
                const chartResult = await chartResponse.json();
                
                if (!chartResult.success) {
                    console.error('Chart API Error:', chartResult.error);
                    return;
                }
                
                const chartData = chartResult.data.chart_data;
                const stats = chartData.statistics;
                
                // Update current stats display
                const currentValue = chartData.data_points.length > 0 
                    ? chartData.data_points[chartData.data_points.length - 1][1] 
                    : null;
                
                document.getElementById('currentNoise').textContent = 
                    currentValue ? `${currentValue.toFixed(1)} dBm` : '-- dBm';
                document.getElementById('avgNoise').textContent = `${stats.average.toFixed(1)} dBm`;
                document.getElementById('minNoise').textContent = `${stats.min} dBm`;
                document.getElementById('maxNoise').textContent = `${stats.max} dBm`;
                
                // Create or update chart
                if (!chart) {
                    createChart(chartData);
                } else {
                    // Check if we have new data
                    const latestTimestamp = chartData.data_points.length > 0 
                        ? chartData.data_points[chartData.data_points.length - 1][0] 
                        : 0;
                    
                    if (latestTimestamp > lastUpdateTime) {
                        chart.data.datasets[0].data = chartData.data_points;
                        chart.data.datasets[1].data = chartData.data_points.map(point => [point[0], stats.average]);
                        chart.update('none');
                        lastUpdateTime = latestTimestamp;
                    }
                }
                
            } catch (error) {
                console.error('Error updating noise floor data:', error);
            }
        }
        
        function createChart(chartData) {
            const ctx = document.getElementById('realTimeChart').getContext('2d');
            const stats = chartData.statistics;
            
            chart = new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: [{
                        label: 'Noise Floor',
                        data: chartData.data_points,
                        borderColor: '#FF6384',
                        backgroundColor: '#FF638420',
                        fill: true,
                        tension: 0.4,
                        pointRadius: 2,
                        pointHoverRadius: 5
                    }, {
                        label: 'Average',
                        data: chartData.data_points.map(point => [point[0], stats.average]),
                        borderColor: '#36A2EB',
                        borderDash: [5, 5],
                        pointRadius: 0,
                        fill: false
                    }]
                },
                options: {
                    responsive: true,
                    animation: {
                        duration: 750
                    },
                    plugins: {
                        title: {
                            display: true,
                            text: 'Noise Floor - Last Hour (Real-time)'
                        }
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                displayFormats: {
                                    minute: 'HH:mm'
                                }
                            },
                            title: {
                                display: true,
                                text: 'Time'
                            }
                        },
                        y: {
                            title: {
                                display: true,
                                text: 'Noise Floor (dBm)'
                            },
                            min: Math.min(stats.min - 5, -120),
                            max: Math.max(stats.max + 5, -60)
                        }
                    }
                }
            });
            
            lastUpdateTime = chartData.data_points.length > 0 
                ? chartData.data_points[chartData.data_points.length - 1][0] 
                : 0;
        }
        
        // Initial load
        updateNoiseFloorData();
        
        // Update every 30 seconds
        setInterval(updateNoiseFloorData, 30000);
    </script>
</body>
</html>
```

## Chart.js Examples

### 1. Packet Type Distribution (Pie Chart)

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <canvas id="packetTypeChart" width="400" height="400"></canvas>
    
    <script>
        async function createPacketTypePieChart() {
            const response = await fetch('/api/packet_type_stats?hours=24');
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            const data = result.data.packet_type_totals;
            const labels = Object.keys(data);
            const values = Object.values(data);
            
            // Filter out zero values
            const filteredData = labels.map((label, index) => ({
                label: label,
                value: values[index]
            })).filter(item => item.value > 0);
            
            const ctx = document.getElementById('packetTypeChart').getContext('2d');
            new Chart(ctx, {
                type: 'pie',
                data: {
                    labels: filteredData.map(item => item.label),
                    datasets: [{
                        data: filteredData.map(item => item.value),
                        backgroundColor: [
                            '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0',
                            '#9966FF', '#FF9F40', '#FF6384', '#C9CBCF',
                            '#4BC0C0', '#FF6384'
                        ]
                    }]
                },
                options: {
                    responsive: true,
                    plugins: {
                        title: {
                            display: true,
                            text: 'Packet Type Distribution (Last 24 Hours)'
                        }
                    }
                }
            });
        }
        
        createPacketTypePieChart();
    </script>
</body>
</html>
```

### 2. Packet Types Over Time (Line Chart)

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
</head>
<body>
    <canvas id="packetTypeTimeChart" width="800" height="400"></canvas>
    
    <script>
        async function createPacketTypeTimeChart() {
            const response = await fetch('/api/packet_type_graph_data?hours=24&types=0,1,2,3,4');
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            const datasets = result.data.series.map((series, index) => {
                const colors = ['#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF'];
                return {
                    label: series.name,
                    data: series.data,
                    borderColor: colors[index % colors.length],
                    backgroundColor: colors[index % colors.length] + '20',
                    fill: false,
                    tension: 0.1
                };
            });
            
            const ctx = document.getElementById('packetTypeTimeChart').getContext('2d');
            new Chart(ctx, {
                type: 'line',
                data: { datasets },
                options: {
                    responsive: true,
                    plugins: {
                        title: {
                            display: true,
                            text: 'Packet Types Over Time (Packets per Minute)'
                        }
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                displayFormats: {
                                    hour: 'MMM dd HH:mm'
                                }
                            }
                        },
                        y: {
                            beginAtZero: true,
                            title: {
                                display: true,
                                text: 'Packets per Minute'
                            }
                        }
                    }
                }
            });
        }
        
        createPacketTypeTimeChart();
    </script>
</body>
</html>
```

### 3. Network Metrics Dashboard (Multiple Charts)

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        .dashboard { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .chart-container { position: relative; height: 400px; }
    </style>
</head>
<body>
    <div class="dashboard">
        <div class="chart-container">
            <canvas id="rssiChart"></canvas>
        </div>
        <div class="chart-container">
            <canvas id="snrChart"></canvas>
        </div>
        <div class="chart-container">
            <canvas id="packetRateChart"></canvas>
        </div>
        <div class="chart-container">
            <canvas id="neighborChart"></canvas>
        </div>
    </div>
    
    <script>
        async function createMetricsDashboard() {
            // Get metrics data for the last 6 hours
            const response = await fetch('/api/metrics_graph_data?hours=6');
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            const series = result.data.series;
            
            // Helper function to find series by type
            function findSeries(type) {
                return series.find(s => s.type === type);
            }
            
            // RSSI Chart
            const rssiData = findSeries('avg_rssi');
            if (rssiData) {
                new Chart(document.getElementById('rssiChart'), {
                    type: 'line',
                    data: {
                        datasets: [{
                            label: rssiData.name,
                            data: rssiData.data,
                            borderColor: '#FF6384',
                            backgroundColor: '#FF638420',
                            fill: true
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            title: { display: true, text: 'Average RSSI' }
                        },
                        scales: {
                            x: { type: 'time' },
                            y: { 
                                title: { display: true, text: 'RSSI (dBm)' },
                                min: -120,
                                max: -30
                            }
                        }
                    }
                });
            }
            
            // SNR Chart
            const snrData = findSeries('avg_snr');
            if (snrData) {
                new Chart(document.getElementById('snrChart'), {
                    type: 'line',
                    data: {
                        datasets: [{
                            label: snrData.name,
                            data: snrData.data,
                            borderColor: '#36A2EB',
                            backgroundColor: '#36A2EB20',
                            fill: true
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            title: { display: true, text: 'Average SNR' }
                        },
                        scales: {
                            x: { type: 'time' },
                            y: { 
                                title: { display: true, text: 'SNR (dB)' },
                                min: -10,
                                max: 15
                            }
                        }
                    }
                });
            }
            
            // Packet Rate Chart (RX/TX)
            const rxData = findSeries('rx_count');
            const txData = findSeries('tx_count');
            if (rxData && txData) {
                new Chart(document.getElementById('packetRateChart'), {
                    type: 'line',
                    data: {
                        datasets: [{
                            label: 'Received',
                            data: rxData.data,
                            borderColor: '#4BC0C0',
                            backgroundColor: '#4BC0C020',
                            fill: false
                        }, {
                            label: 'Transmitted',
                            data: txData.data,
                            borderColor: '#FFCE56',
                            backgroundColor: '#FFCE5620',
                            fill: false
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            title: { display: true, text: 'Packet Rate' }
                        },
                        scales: {
                            x: { type: 'time' },
                            y: { 
                                beginAtZero: true,
                                title: { display: true, text: 'Packets per Minute' }
                            }
                        }
                    }
                });
            }
            
            // Neighbor Count Chart
            const neighborData = findSeries('neighbor_count');
            if (neighborData) {
                new Chart(document.getElementById('neighborChart'), {
                    type: 'line',
                    data: {
                        datasets: [{
                            label: neighborData.name,
                            data: neighborData.data,
                            borderColor: '#9966FF',
                            backgroundColor: '#9966FF20',
                            fill: true,
                            stepped: true
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            title: { display: true, text: 'Neighbor Count' }
                        },
                        scales: {
                            x: { type: 'time' },
                            y: { 
                                beginAtZero: true,
                                title: { display: true, text: 'Number of Neighbors' },
                                ticks: { stepSize: 1 }
                            }
                        }
                    }
                });
            }
        }
        
        createMetricsDashboard();
    </script>
</body>
</html>
```

## Plotly.js Examples

### 1. Interactive 3D Packet Type Surface

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
</head>
<body>
    <div id="packet3dChart" style="width:100%;height:500px;"></div>
    
    <script>
        async function create3DPacketChart() {
            const response = await fetch('/api/packet_type_graph_data?hours=168'); // 1 week
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            // Prepare data for 3D surface plot
            const traces = result.data.series.map(series => ({
                x: series.data.map(point => new Date(point[0])),
                y: series.data.map(point => point[1]),
                name: series.name,
                type: 'scatter3d',
                mode: 'markers+lines',
                marker: {
                    size: 3,
                    opacity: 0.8
                },
                line: {
                    width: 2
                }
            }));
            
            const layout = {
                title: 'Packet Types in 3D Space (Last Week)',
                scene: {
                    xaxis: { title: 'Time' },
                    yaxis: { title: 'Packet Rate' },
                    zaxis: { title: 'Packet Type' }
                }
            };
            
            Plotly.newPlot('packet3dChart', traces, layout);
        }
        
        create3DPacketChart();
    </script>
</body>
</html>
```

### 2. Heatmap of Packet Activity

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
</head>
<body>
    <div id="heatmapChart" style="width:100%;height:600px;"></div>
    
    <script>
        async function createPacketHeatmap() {
            const response = await fetch('/api/packet_type_graph_data?hours=168&types=0,1,2,3,4');
            const result = await response.json();
            
            if (!result.success) {
                console.error('API Error:', result.error);
                return;
            }
            
            // Convert data to heatmap format
            const series = result.data.series;
            const timestamps = series[0].data.map(point => new Date(point[0]));
            const packetTypes = series.map(s => s.name);
            
            // Create z-matrix for heatmap
            const z = series.map(s => s.data.map(point => point[1]));
            
            const trace = {
                z: z,
                x: timestamps,
                y: packetTypes,
                type: 'heatmap',
                colorscale: 'Viridis',
                hoverongaps: false
            };
            
            const layout = {
                title: 'Packet Activity Heatmap (Last Week)',
                xaxis: { title: 'Time' },
                yaxis: { title: 'Packet Type' }
            };
            
            Plotly.newPlot('heatmapChart', [trace], layout);
        }
        
        createPacketHeatmap();
    </script>
</body>
</html>
```

## D3.js Example

### Real-time Network Status

```html
<!DOCTYPE html>
<html>
<head>
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        .network-node { fill: #4BC0C0; stroke: #fff; stroke-width: 2px; }
        .network-link { stroke: #999; stroke-opacity: 0.6; }
        .metric-text { font-size: 12px; fill: #333; }
    </style>
</head>
<body>
    <svg id="networkChart" width="800" height="600"></svg>
    
    <script>
        async function createNetworkVisualization() {
            const statsResponse = await fetch('/api/packet_stats?hours=1');
            const metricsResponse = await fetch('/api/metrics_graph_data?hours=1&metrics=avg_rssi,avg_snr,neighbor_count');
            
            const statsResult = await statsResponse.json();
            const metricsResult = await metricsResponse.json();
            
            if (!statsResult.success || !metricsResult.success) {
                console.error('API Error');
                return;
            }
            
            const svg = d3.select("#networkChart");
            
            // Create network nodes based on data
            const centerNode = {
                id: 'repeater',
                x: 400,
                y: 300,
                r: 30,
                type: 'repeater'
            };
            
            // Create neighbor nodes based on neighbor count
            const neighborCount = metricsResult.data.series.find(s => s.type === 'neighbor_count')?.data.slice(-1)[0]?.[1] || 0;
            const neighborNodes = d3.range(neighborCount).map(i => ({
                id: `neighbor_${i}`,
                x: 400 + 150 * Math.cos(2 * Math.PI * i / neighborCount),
                y: 300 + 150 * Math.sin(2 * Math.PI * i / neighborCount),
                r: 15,
                type: 'neighbor'
            }));
            
            const nodes = [centerNode, ...neighborNodes];
            const links = neighborNodes.map(n => ({ source: centerNode, target: n }));
            
            // Draw links
            svg.selectAll('.network-link')
                .data(links)
                .enter().append('line')
                .attr('class', 'network-link')
                .attr('x1', d => d.source.x)
                .attr('y1', d => d.source.y)
                .attr('x2', d => d.target.x)
                .attr('y2', d => d.target.y);
            
            // Draw nodes
            svg.selectAll('.network-node')
                .data(nodes)
                .enter().append('circle')
                .attr('class', 'network-node')
                .attr('cx', d => d.x)
                .attr('cy', d => d.y)
                .attr('r', d => d.r)
                .style('fill', d => d.type === 'repeater' ? '#FF6384' : '#4BC0C0');
            
            // Add labels
            svg.selectAll('.network-label')
                .data(nodes)
                .enter().append('text')
                .attr('class', 'metric-text')
                .attr('x', d => d.x)
                .attr('y', d => d.y + 5)
                .attr('text-anchor', 'middle')
                .text(d => d.type === 'repeater' ? 'Repeater' : `N${d.id.split('_')[1]}`);
            
            // Add metrics display
            const avgRssi = metricsResult.data.series.find(s => s.type === 'avg_rssi')?.data.slice(-1)[0]?.[1] || 0;
            const avgSnr = metricsResult.data.series.find(s => s.type === 'avg_snr')?.data.slice(-1)[0]?.[1] || 0;
            
            svg.append('text')
                .attr('x', 20)
                .attr('y', 30)
                .attr('class', 'metric-text')
                .style('font-size', '16px')
                .text(`Network Status - RSSI: ${avgRssi.toFixed(1)} dBm, SNR: ${avgSnr.toFixed(1)} dB`);
            
            svg.append('text')
                .attr('x', 20)
                .attr('y', 50)
                .attr('class', 'metric-text')
                .text(`Total Packets (last hour): ${statsResult.data.total_packets}`);
        }
        
        createNetworkVisualization();
        
        // Update every 30 seconds
        setInterval(createNetworkVisualization, 30000);
    </script>
</body>
</html>
```

## Vue.js Framework Integration

### Vue.js Component Example

```vue
<template>
  <div class="network-metrics-chart">
    <div class="controls">
      <label for="timeRange">Time Range: </label>
      <select 
        id="timeRange" 
        v-model="timeRange" 
        @change="fetchMetricsData"
        class="time-select"
      >
        <option value="1">1 Hour</option>
        <option value="6">6 Hours</option>
        <option value="24">24 Hours</option>
        <option value="168">1 Week</option>
      </select>
      
      <label for="metrics">Metrics: </label>
      <select 
        id="metrics" 
        v-model="selectedMetrics" 
        @change="fetchMetricsData"
        multiple
        class="metrics-select"
      >
        <option value="avg_rssi">Average RSSI</option>
        <option value="avg_snr">Average SNR</option>
        <option value="rx_count">Received Packets</option>
        <option value="tx_count">Transmitted Packets</option>
        <option value="neighbor_count">Neighbor Count</option>
      </select>
    </div>
    
    <div v-if="loading" class="loading">
      Loading metrics data...
    </div>
    
    <div v-else-if="error" class="error">
      {{ error }}
    </div>
    
    <canvas 
      v-else-if="chartData" 
      ref="chartCanvas" 
      width="800" 
      height="400"
    ></canvas>
    
    <div v-else class="no-data">
      No data available
    </div>
  </div>
</template>

<script>
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  TimeScale
} from 'chart.js';
import { Line } from 'vue-chartjs';
import 'chartjs-adapter-date-fns';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  TimeScale
);

export default {
  name: 'NetworkMetricsChart',
  data() {
    return {
      chartData: null,
      chartInstance: null,
      timeRange: 24,
      selectedMetrics: ['avg_rssi', 'avg_snr'],
      loading: false,
      error: null,
      colors: {
        avg_rssi: '#FF6384',
        avg_snr: '#36A2EB',
        rx_count: '#4BC0C0',
        tx_count: '#FFCE56',
        neighbor_count: '#9966FF'
      }
    };
  },
  mounted() {
    this.fetchMetricsData();
  },
  beforeUnmount() {
    if (this.chartInstance) {
      this.chartInstance.destroy();
    }
  },
  methods: {
    async fetchMetricsData() {
      this.loading = true;
      this.error = null;
      
      try {
        const metricsParam = this.selectedMetrics.join(',');
        const response = await fetch(
          `/api/metrics_graph_data?hours=${this.timeRange}&metrics=${metricsParam}`
        );
        const result = await response.json();
        
        if (result.success) {
          this.updateChart(result.data);
        } else {
          this.error = result.error || 'Failed to fetch data';
        }
      } catch (err) {
        this.error = 'Network error: ' + err.message;
        console.error('Error fetching metrics:', err);
      } finally {
        this.loading = false;
      }
    },
    
    updateChart(data) {
      const datasets = data.series.map(series => ({
        label: series.name,
        data: series.data,
        borderColor: this.colors[series.type] || '#999999',
        backgroundColor: (this.colors[series.type] || '#999999') + '20',
        fill: false,
        tension: 0.1
      }));
      
      this.chartData = { datasets };
      
      // Destroy existing chart if it exists
      if (this.chartInstance) {
        this.chartInstance.destroy();
      }
      
      // Create new chart
      this.$nextTick(() => {
        const ctx = this.$refs.chartCanvas.getContext('2d');
        this.chartInstance = new ChartJS(ctx, {
          type: 'line',
          data: this.chartData,
          options: this.getChartOptions()
        });
      });
    },
    
    getChartOptions() {
      return {
        responsive: true,
        plugins: {
          legend: {
            position: 'top'
          },
          title: {
            display: true,
            text: `Network Metrics (Last ${this.timeRange} Hours)`
          }
        },
        scales: {
          x: {
            type: 'time',
            time: {
              displayFormats: {
                hour: 'MMM dd HH:mm',
                day: 'MMM dd'
              }
            },
            title: {
              display: true,
              text: 'Time'
            }
          },
          y: {
            title: {
              display: true,
              text: 'Value'
            }
          }
        },
        interaction: {
          intersect: false,
          mode: 'index'
        }
      };
    }
  }
};
</script>

<style scoped>
.network-metrics-chart {
  padding: 20px;
}

.controls {
  margin-bottom: 20px;
  display: flex;
  gap: 15px;
  align-items: center;
  flex-wrap: wrap;
}

.controls label {
  font-weight: bold;
}

.time-select, .metrics-select {
  padding: 8px;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 14px;
}

.metrics-select {
  min-width: 200px;
  min-height: 80px;
}

.loading {
  text-align: center;
  padding: 40px;
  color: #666;
}

.error {
  background-color: #fee;
  color: #c33;
  padding: 15px;
  border-radius: 4px;
  border: 1px solid #fcc;
}

.no-data {
  text-align: center;
  padding: 40px;
  color: #999;
}
</style>
```

### Vue.js Packet Type Dashboard

```vue
<template>
  <div class="packet-dashboard">
    <h2>Packet Type Analysis Dashboard</h2>
    
    <div class="dashboard-controls">
      <div class="control-group">
        <label>Time Period:</label>
        <select v-model="timePeriod" @change="fetchAllData">
          <option value="1">Last Hour</option>
          <option value="6">Last 6 Hours</option>
          <option value="24">Last 24 Hours</option>
          <option value="168">Last Week</option>
        </select>
      </div>
      
      <div class="control-group">
        <label>Auto Refresh:</label>
        <input 
          type="checkbox" 
          v-model="autoRefresh" 
          @change="toggleAutoRefresh"
        />
        <span v-if="autoRefresh">({{ refreshCountdown }}s)</span>
      </div>
    </div>

    <div class="dashboard-grid">
      <!-- Packet Type Distribution -->
      <div class="chart-container">
        <h3>Packet Type Distribution</h3>
        <canvas ref="pieChart" width="400" height="400"></canvas>
      </div>

      <!-- Packet Types Over Time -->
      <div class="chart-container">
        <h3>Packet Types Over Time</h3>
        <canvas ref="timeChart" width="600" height="400"></canvas>
      </div>

      <!-- Statistics Cards -->
      <div class="stats-container">
        <div class="stat-card" v-for="stat in stats" :key="stat.label">
          <div class="stat-label">{{ stat.label }}</div>
          <div class="stat-value">{{ stat.value }}</div>
        </div>
      </div>

      <!-- Recent Packets Table -->
      <div class="recent-packets">
        <h3>Recent Packets</h3>
        <div class="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Type</th>
                <th>Route</th>
                <th>RSSI</th>
                <th>SNR</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="packet in recentPackets" :key="packet.timestamp">
                <td>{{ formatTime(packet.timestamp) }}</td>
                <td>{{ getPacketTypeName(packet.type) }}</td>
                <td>{{ packet.route }}</td>
                <td>{{ packet.rssi }}</td>
                <td>{{ packet.snr?.toFixed(1) }}</td>
                <td>
                  <span :class="packet.transmitted ? 'status-ok' : 'status-dropped'">
                    {{ packet.transmitted ? 'TX' : 'DROP' }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { Chart as ChartJS, ArcElement, Tooltip, Legend } from 'chart.js';

ChartJS.register(ArcElement, Tooltip, Legend);

export default {
  name: 'PacketDashboard',
  data() {
    return {
      timePeriod: 24,
      autoRefresh: false,
      refreshInterval: null,
      refreshCountdown: 30,
      pieChartInstance: null,
      timeChartInstance: null,
      stats: [],
      recentPackets: [],
      packetTypeNames: {
        0: 'Request (REQ)',
        1: 'Response (RESPONSE)',
        2: 'Text Message (TXT_MSG)',
        3: 'ACK (ACK)',
        4: 'Advert (ADVERT)',
        5: 'Group Text (GRP_TXT)',
        6: 'Group Data (GRP_DATA)',
        7: 'Anonymous Request (ANON_REQ)',
        8: 'Path (PATH)',
        9: 'Trace (TRACE)'
      }
    };
  },
  mounted() {
    this.fetchAllData();
  },
  beforeUnmount() {
    this.clearRefreshInterval();
    if (this.pieChartInstance) this.pieChartInstance.destroy();
    if (this.timeChartInstance) this.timeChartInstance.destroy();
  },
  methods: {
    async fetchAllData() {
      await Promise.all([
        this.fetchPacketTypeStats(),
        this.fetchPacketTypeGraphData(),
        this.fetchRecentPackets()
      ]);
    },

    async fetchPacketTypeStats() {
      try {
        const response = await fetch(`/api/packet_type_stats?hours=${this.timePeriod}`);
        const result = await response.json();
        
        if (result.success) {
          this.updatePieChart(result.data);
          this.updateStats(result.data);
        }
      } catch (error) {
        console.error('Error fetching packet type stats:', error);
      }
    },

    async fetchPacketTypeGraphData() {
      try {
        const response = await fetch(`/api/packet_type_graph_data?hours=${this.timePeriod}&types=0,1,2,3,4`);
        const result = await response.json();
        
        if (result.success) {
          this.updateTimeChart(result.data);
        }
      } catch (error) {
        console.error('Error fetching graph data:', error);
      }
    },

    async fetchRecentPackets() {
      try {
        const response = await fetch('/api/recent_packets?limit=20');
        const result = await response.json();
        
        if (result.success) {
          this.recentPackets = result.data;
        }
      } catch (error) {
        console.error('Error fetching recent packets:', error);
      }
    },

    updatePieChart(data) {
      const filteredData = Object.entries(data.packet_type_totals)
        .filter(([_, value]) => value > 0)
        .sort(([_, a], [__, b]) => b - a);

      const chartData = {
        labels: filteredData.map(([label, _]) => label),
        datasets: [{
          data: filteredData.map(([_, value]) => value),
          backgroundColor: [
            '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0',
            '#9966FF', '#FF9F40', '#FF6384', '#C9CBCF'
          ]
        }]
      };

      if (this.pieChartInstance) {
        this.pieChartInstance.destroy();
      }

      this.$nextTick(() => {
        const ctx = this.$refs.pieChart.getContext('2d');
        this.pieChartInstance = new ChartJS(ctx, {
          type: 'pie',
          data: chartData,
          options: {
            responsive: true,
            plugins: {
              legend: { position: 'right' },
              title: {
                display: true,
                text: `Distribution (${this.timePeriod}h)`
              }
            }
          }
        });
      });
    },

    updateTimeChart(data) {
      const datasets = data.series.map((series, index) => {
        const colors = ['#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF'];
        return {
          label: series.name,
          data: series.data,
          borderColor: colors[index % colors.length],
          backgroundColor: colors[index % colors.length] + '20',
          fill: false,
          tension: 0.1
        };
      });

      if (this.timeChartInstance) {
        this.timeChartInstance.destroy();
      }

      this.$nextTick(() => {
        const ctx = this.$refs.timeChart.getContext('2d');
        this.timeChartInstance = new ChartJS(ctx, {
          type: 'line',
          data: { datasets },
          options: {
            responsive: true,
            plugins: {
              title: {
                display: true,
                text: 'Packet Rate Over Time'
              }
            },
            scales: {
              x: { type: 'time' },
              y: { 
                beginAtZero: true,
                title: { display: true, text: 'Packets/min' }
              }
            }
          }
        });
      });
    },

    updateStats(data) {
      this.stats = [
        { label: 'Total Packets', value: data.total_packets },
        { label: 'Time Period', value: `${data.hours} hours` },
        { label: 'Most Common', value: this.getMostCommonPacketType(data.packet_type_totals) },
        { label: 'Types Active', value: Object.values(data.packet_type_totals).filter(v => v > 0).length }
      ];
    },

    getMostCommonPacketType(totals) {
      const sorted = Object.entries(totals).sort(([_, a], [__, b]) => b - a);
      return sorted[0] ? sorted[0][0] : 'None';
    },

    getPacketTypeName(type) {
      return this.packetTypeNames[type] || `Unknown (${type})`;
    },

    formatTime(timestamp) {
      return new Date(timestamp * 1000).toLocaleTimeString();
    },

    toggleAutoRefresh() {
      if (this.autoRefresh) {
        this.startAutoRefresh();
      } else {
        this.clearRefreshInterval();
      }
    },

    startAutoRefresh() {
      this.refreshCountdown = 30;
      this.refreshInterval = setInterval(() => {
        this.refreshCountdown--;
        if (this.refreshCountdown <= 0) {
          this.fetchAllData();
          this.refreshCountdown = 30;
        }
      }, 1000);
    },

    clearRefreshInterval() {
      if (this.refreshInterval) {
        clearInterval(this.refreshInterval);
        this.refreshInterval = null;
      }
    }
  }
};
</script>

<style scoped>
.packet-dashboard {
  padding: 20px;
  max-width: 1400px;
  margin: 0 auto;
}

.dashboard-controls {
  display: flex;
  gap: 20px;
  margin-bottom: 30px;
  padding: 15px;
  background: #f8f9fa;
  border-radius: 8px;
}

.control-group {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-gap: 20px;
}

.chart-container {
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

.stats-container {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 15px;
  grid-column: span 2;
}

.stat-card {
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
  text-align: center;
}

.stat-label {
  font-size: 14px;
  color: #666;
  margin-bottom: 8px;
}

.stat-value {
  font-size: 24px;
  font-weight: bold;
  color: #333;
}

.recent-packets {
  grid-column: span 2;
  background: white;
  padding: 20px;
  border-radius: 8px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

.table-wrapper {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 15px;
}

th, td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid #ddd;
}

th {
  background-color: #f8f9fa;
  font-weight: bold;
}

.status-ok {
  color: #28a745;
  font-weight: bold;
}

.status-dropped {
  color: #dc3545;
  font-weight: bold;
}

@media (max-width: 768px) {
  .dashboard-grid {
    grid-template-columns: 1fr;
  }
  
  .stats-container {
    grid-column: span 1;
    grid-template-columns: 1fr 1fr;
  }
  
  .recent-packets {
    grid-column: span 1;
  }
}
</style>
```

### Composable for API Management (Vue 3 Composition API)

```javascript
// composables/useNetworkAPI.js
import { ref, reactive } from 'vue';

export function useNetworkAPI() {
  const loading = ref(false);
  const error = ref(null);
  
  const cache = reactive(new Map());
  const CACHE_TTL = 60000; // 1 minute
  
  async function fetchWithCache(url, forceRefresh = false) {
    const cacheKey = url;
    const now = Date.now();
    
    // Check cache first
    if (!forceRefresh && cache.has(cacheKey)) {
      const cached = cache.get(cacheKey);
      if (now - cached.timestamp < CACHE_TTL) {
        return cached.data;
      }
    }
    
    loading.value = true;
    error.value = null;
    
    try {
      const response = await fetch(url);
      const result = await response.json();
      
      if (!result.success) {
        throw new Error(result.error || 'API request failed');
      }
      
      // Cache the result
      cache.set(cacheKey, {
        data: result.data,
        timestamp: now
      });
      
      return result.data;
    } catch (err) {
      error.value = err.message;
      console.error('API Error:', err);
      return null;
    } finally {
      loading.value = false;
    }
  }
  
  async function getPacketStats(hours = 24) {
    return await fetchWithCache(`/api/packet_stats?hours=${hours}`);
  }
  
  async function getPacketTypeStats(hours = 24) {
    return await fetchWithCache(`/api/packet_type_stats?hours=${hours}`);
  }
  
  async function getPacketTypeGraphData(hours = 24, types = 'all') {
    const typesParam = types === 'all' ? '' : `&types=${types}`;
    return await fetchWithCache(`/api/packet_type_graph_data?hours=${hours}${typesParam}`);
  }
  
  async function getMetricsGraphData(hours = 24, metrics = 'all') {
    const metricsParam = metrics === 'all' ? '' : `&metrics=${metrics}`;
    return await fetchWithCache(`/api/metrics_graph_data?hours=${hours}${metricsParam}`);
  }
  
  async function getRecentPackets(limit = 100) {
    return await fetchWithCache(`/api/recent_packets?limit=${limit}`);
  }
  
  async function getFilteredPackets(filters = {}) {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== null && value !== undefined) {
        params.append(key, value);
      }
    });
    
    return await fetchWithCache(`/api/filtered_packets?${params.toString()}`);
  }
  
  function clearCache() {
    cache.clear();
  }
  
  return {
    loading,
    error,
    getPacketStats,
    getPacketTypeStats,
    getPacketTypeGraphData,
    getMetricsGraphData,
    getRecentPackets,
    getFilteredPackets,
    clearCache
  };
}
```

### Simple Vue.js Usage Example

```vue
<template>
  <div id="app">
    <h1>Network Monitoring Dashboard</h1>
    
    <!-- Quick Stats -->
    <div class="quick-stats" v-if="stats">
      <div class="stat-item">
        <span class="label">Total Packets:</span>
        <span class="value">{{ stats.total_packets }}</span>
      </div>
      <div class="stat-item">
        <span class="label">Avg RSSI:</span>
        <span class="value">{{ stats.avg_rssi }} dBm</span>
      </div>
      <div class="stat-item">
        <span class="label">Avg SNR:</span>
        <span class="value">{{ stats.avg_snr }} dB</span>
      </div>
    </div>
    
    <!-- Charts -->
    <NetworkMetricsChart />
    <PacketDashboard />
  </div>
</template>

<script>
import NetworkMetricsChart from './components/NetworkMetricsChart.vue';
import PacketDashboard from './components/PacketDashboard.vue';
import { useNetworkAPI } from './composables/useNetworkAPI.js';

export default {
  name: 'App',
  components: {
    NetworkMetricsChart,
    PacketDashboard
  },
  data() {
    return {
      stats: null
    };
  },
  async mounted() {
    const { getPacketStats } = useNetworkAPI();
    this.stats = await getPacketStats(24);
  }
};
</script>

<style>
.quick-stats {
  display: flex;
  gap: 20px;
  margin: 20px 0;
  padding: 20px;
  background: #f8f9fa;
  border-radius: 8px;
}

.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
}

.label {
  font-size: 14px;
  color: #666;
}

.value {
  font-size: 18px;
  font-weight: bold;
  color: #333;
}
</style>
```
```

## API Usage Tips

### 1. Error Handling
```javascript
async function fetchWithErrorHandling(url) {
  try {
    const response = await fetch(url);
    const result = await response.json();
    
    if (!result.success) {
      throw new Error(result.error || 'API request failed');
    }
    
    return result.data;
  } catch (error) {
    console.error('API Error:', error);
    return null;
  }
}
```

### 2. Caching Strategy
```javascript
class APICache {
  constructor(ttl = 60000) { // 1 minute TTL
    this.cache = new Map();
    this.ttl = ttl;
  }
  
  async get(key, fetcher) {
    const now = Date.now();
    const cached = this.cache.get(key);
    
    if (cached && (now - cached.timestamp) < this.ttl) {
      return cached.data;
    }
    
    const data = await fetcher();
    this.cache.set(key, { data, timestamp: now });
    return data;
  }
}

const apiCache = new APICache();

// Usage
const data = await apiCache.get('metrics-24h', () => 
  fetch('/api/metrics_graph_data?hours=24').then(r => r.json())
);
```

### 3. Real-time Updates
```javascript
function setupRealTimeUpdates(chartComponent, interval = 30000) {
  const updateChart = async () => {
    const data = await fetchWithErrorHandling('/api/metrics_graph_data?hours=1');
    if (data) {
      chartComponent.updateData(data);
    }
  };
  
  // Initial load
  updateChart();
  
  // Set up interval
  const intervalId = setInterval(updateChart, interval);
  
  // Return cleanup function
  return () => clearInterval(intervalId);
}
```

This documentation provides examples for creating various types of charts and visualizations using the pyMC_Repeater API endpoints. The examples cover different chart libraries and use cases, from simple statistics to real-time network monitoring.