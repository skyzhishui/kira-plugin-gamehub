/* shared/cleankey.js — 运行时色键：把素材里烙进图里的「平涂底色」抠成真透明，
 * 只处理内存中的位图，不改素材文件。
 *
 * 现在的调用方（KiraAI 版只剩通用素材，角色立绘不再随插件分发）：
 * - `./assets/agent-avatar.png`：座位头像。该图自带 alpha 通道，函数据此直接
 *   跳过（透明像素 >1% 即视为已有 alpha），不会把浅色衣褶误抠掉；
 * - `./assets/card-gomoku.png`：大厅棋盘封面，平涂近白底色 —— 需要抠底时按
 *   调用方传入的 key（默认近白 253,253,253）处理。
 *
 * 平涂底色非常均匀（±4），绘画内容有渐变与描边，紧容差欧氏距离即可安全分离；
 * 带比例保险丝：只有确实抠掉大面积（>8%）才替换，防止误伤其它图片。
 * 任一步失败（CASP 污染、canvas 不可用等）都静默保留原图。
 */

const SKY_KEY = [177, 211, 252]; // 浅天蓝平涂（卡片/招牌类插画）
const PAPER_KEY = [253, 253, 253]; // 近白平涂（card-gomoku.png 一类棋盘底）
const HARD = 9; // 硬抠：距离内全透明
const SOFT = 20; // 软边：过渡到不透明，避免描边白边

/**
 * @param {HTMLImageElement} img 已插入文档的图片（原地替换 src 为透明版本）
 * @param {{key?: number[]}} [opts] key 为目标平涂底色，缺省浅天蓝
 */
export async function removeFlatBackdrop(img, { key = SKY_KEY } = {}) {
  const KEY = Array.isArray(key) && key.length === 3 ? key : SKY_KEY;
  try {
    if (!img || !img.isConnected) return img;
    if (!img.complete || !img.naturalWidth) {
      await new Promise((resolve, reject) => {
        img.addEventListener("load", resolve, { once: true });
        img.addEventListener("error", reject, { once: true });
      });
    }
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    if (!w || !h) return img;

    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const id = ctx.getImageData(0, 0, w, h);
    const d = id.data;
    // 已是透明底素材（如生成图带 alpha）：直接保留，避免把浅蓝发色/白色衣褶
    // 误判成背景色键。透明像素占比 >1% 即视为已有 alpha 通道。
    let transparent = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] === 0) transparent += 1;
    if (transparent > w * h * 0.01) return img;
    let keyed = 0;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] === 0) continue;
      const dr = d[i] - KEY[0];
      const dg = d[i + 1] - KEY[1];
      const db = d[i + 2] - KEY[2];
      const dist = Math.sqrt(dr * dr + dg * dg + db * db);
      if (dist < HARD) {
        d[i + 3] = 0;
        keyed += 1;
      } else if (dist < SOFT) {
        d[i + 3] = Math.round(d[i + 3] * ((dist - HARD) / (SOFT - HARD)));
        keyed += 1;
      }
    }
    if (keyed > w * h * 0.08) {
      ctx.putImageData(id, 0, 0);
      img.src = c.toDataURL("image/png");
    }
  } catch {
    /* 保留原图 */
  }
  return img;
}

export { PAPER_KEY };