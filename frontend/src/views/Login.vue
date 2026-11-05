<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <el-icon :size="40" color="#c8a45c"><TrendCharts /></el-icon>
        <h1>衡策</h1>
        <p>AI 驱动的 A 股智能投研平台</p>
      </div>

      <!-- 登录 -->
      <el-form v-if="mode === 'login'" :model="loginForm" :rules="loginRules" ref="loginRef" label-width="0">
        <el-form-item prop="username">
          <el-input v-model="loginForm.username" placeholder="用户名" :prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="loginForm.password" type="password" placeholder="密码" :prefix-icon="Lock" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" :loading="loading" @click="handleLogin" style="width: 100%">
            登录
          </el-button>
        </el-form-item>
        <div class="switch-link">
          还没有账号？<a @click="mode = 'register'">立即注册</a>
        </div>
      </el-form>

      <!-- 注册 -->
      <el-form v-else :model="regForm" :rules="regRules" ref="regRef" label-width="0">
        <el-form-item prop="username">
          <el-input v-model="regForm.username" placeholder="用户名 (≥3位)" :prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item prop="email">
          <el-input v-model="regForm.email" placeholder="邮箱" :prefix-icon="Message" size="large" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="regForm.password" type="password" placeholder="密码 (≥6位)" :prefix-icon="Lock" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-select v-model="regForm.risk_level" placeholder="风险偏好" size="large" style="width: 100%">
            <el-option label="保守型" value="conservative" />
            <el-option label="稳健型" value="moderate" />
            <el-option label="平衡型" value="balanced" />
            <el-option label="积极型" value="aggressive" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" :loading="loading" @click="handleRegister" style="width: 100%">
            注册并登录
          </el-button>
        </el-form-item>
        <div class="switch-link">
          已有账号？<a @click="mode = 'login'">返回登录</a>
        </div>
      </el-form>

      <div class="disclaimer">
        ⚠️ 本 AI 助手提供的分析和建议仅供学习参考，不构成任何投资建议。投资有风险，入市需谨慎。
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Message } from '@element-plus/icons-vue'
import { authApi } from '@/api/auth'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const mode = ref<'login' | 'register'>('login')
const loading = ref(false)
const loginRef = ref<FormInstance>()
const regRef = ref<FormInstance>()

const loginForm = reactive({ username: '', password: '' })
const loginRules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const regForm = reactive({ username: '', email: '', password: '', risk_level: 'moderate' })
const regRules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, message: '至少 3 位', trigger: 'blur' },
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '至少 6 位', trigger: 'blur' },
  ],
}

async function handleLogin() {
  if (!loginRef.value) return
  await loginRef.value.validate()
  loading.value = true
  try {
    const data = await authApi.login(loginForm.username, loginForm.password)
    userStore.setAuth(data.token, data.user)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/dashboard'
    router.push(redirect)
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  if (!regRef.value) return
  await regRef.value.validate()
  loading.value = true
  try {
    const data = await authApi.register(regForm.username, regForm.email, regForm.password, regForm.risk_level)
    userStore.setAuth(data.token, data.user)
    ElMessage.success('注册成功，欢迎加入 衡策！')
    router.push('/dashboard')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 券商蓝底 + 金色光晕，贴合金融/投研气质 */
  background:
    radial-gradient(900px 520px at 12% 18%, rgba(200, 164, 92, 0.22) 0%, transparent 62%),
    radial-gradient(700px 500px at 88% 85%, rgba(27, 79, 131, 0.55) 0%, transparent 65%),
    linear-gradient(135deg, #071a2c 0%, #0d2b45 52%, #1f4e79 100%);
}
.login-card {
  width: 400px;
  padding: 40px;
  background: #fff;
  border-radius: 14px;
  border-top: 3px solid #c8a45c;
  box-shadow: 0 20px 60px rgba(3, 15, 27, 0.45);
}
.login-header {
  text-align: center;
  margin-bottom: 30px;
}
.login-header h1 {
  margin: 12px 0 4px;
  font-size: 24px;
  letter-spacing: 4px;
  color: #1f4e79;
}
.login-header p {
  font-size: 13px;
  color: #909399;
}
.switch-link {
  text-align: center;
  font-size: 13px;
  color: #909399;
}
.switch-link a {
  color: #1f4e79;
  font-weight: 600;
  cursor: pointer;
}
.switch-link a:hover {
  text-decoration: underline;
}
.disclaimer {
  margin-top: 20px;
  padding: 10px;
  background: #faf7ee;
  border-left: 3px solid #c8a45c;
  border-radius: 4px;
  font-size: 12px;
  color: #9a7b3c;
  line-height: 1.5;
}
</style>
