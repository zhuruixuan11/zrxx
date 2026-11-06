import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

// ECharts 按需引入：只注册项目实际用到的图表与组件，显著减小打包体积
import { use, registerTheme } from 'echarts/core'
import { LineChart, PieChart, CandlestickChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import ECharts from 'vue-echarts'

import App from './App.vue'
import router from './router'
import './style.css'

use([
  CanvasRenderer,
  LineChart,
  PieChart,
  CandlestickChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
])

// 图表统一主题：券商蓝 + 香槟金 + 红涨绿跌
registerTheme('hengce', {
  color: ['#1f4e79', '#c8a45c', '#d64541', '#2e8b74', '#5b83a8', '#9b7bb8', '#7a8fa6', '#c07a4b'],
})

const app = createApp(App)

// Element Plus 图标全局注册
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.component('v-chart', ECharts)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.mount('#app')
