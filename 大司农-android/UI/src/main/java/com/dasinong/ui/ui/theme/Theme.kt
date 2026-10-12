package com.dasinong.ui.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * 大司农品牌配色 —— 直接取自应用图标（软件图标/大司农.jpg）：
 *
 * · 墨绿（房身 / 屋脊）：#1E4346 → 主色 primary
 * · 金（中央旋纹）      ：#A88347（深）/ #D7B671（亮）→ 强调色 secondary
 * · 奶白（图标底色）    ：#F7EED8 / #FBF6E6 → 背景与表面
 *
 * 全 App 一律用这里的角色色，界面里不再写死十六进制颜色。
 * 图标本身是浅色奶白底，所以这里只做浅色主题（系统栏也由浅色平台主题托底）。
 */
private val DsnTeal = Color(0xFF1E4346)
private val DsnTealSoft = Color(0xFFCFE0DE)
private val DsnGold = Color(0xFFA88347)
private val DsnGoldSoft = Color(0xFFF2E4C4)
private val DsnGreen = Color(0xFF2F6E63)          // 与主色同族的青绿，用作「已连接」
private val DsnGreenSoft = Color(0xFFCBE6E0)
private val DsnCream = Color(0xFFFBF6E6)
private val DsnInk = Color(0xFF1B1C19)

private val DasinongLightColors = lightColorScheme(
    primary = DsnTeal,
    onPrimary = Color.White,
    primaryContainer = DsnTealSoft,
    onPrimaryContainer = Color(0xFF0C2A2B),
    inversePrimary = Color(0xFFA9CFC9),

    secondary = DsnGold,
    onSecondary = Color.White,
    secondaryContainer = DsnGoldSoft,
    onSecondaryContainer = Color(0xFF43330F),

    tertiary = DsnGreen,
    onTertiary = Color.White,
    tertiaryContainer = DsnGreenSoft,
    onTertiaryContainer = Color(0xFF0C302A),

    background = DsnCream,
    onBackground = DsnInk,
    surface = Color(0xFFFFFDF4),
    onSurface = DsnInk,
    surfaceVariant = Color(0xFFE7E1CE),
    onSurfaceVariant = Color(0xFF4A4A40),
    surfaceTint = DsnTeal,

    outline = Color(0xFF7E7B6C),
    outlineVariant = Color(0xFFCFC9B6),

    error = Color(0xFFB3261E),
    onError = Color.White,
    errorContainer = Color(0xFFF9DEDC),
    onErrorContainer = Color(0xFF410E0B),

    // 导航栏 / 卡片等「容器」层级也走奶白系，免得落到默认的冷灰紫
    surfaceDim = Color(0xFFE3DCC7),
    surfaceBright = Color(0xFFFFFDF4),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = DsnCream,
    surfaceContainer = Color(0xFFF5EFDC),
    surfaceContainerHigh = Color(0xFFEFE9D5),
    surfaceContainerHighest = Color(0xFFE9E3CF),
)

@Composable
fun DasinongTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = DasinongLightColors, content = content)
}
