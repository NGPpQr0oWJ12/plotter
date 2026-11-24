const position = "varying vec3 vFabricPosition;";

const microfiber = `
  float fabricCell(vec3 cell) {
    cell = fract(cell * vec3(0.1031, 0.1030, 0.0973));
    cell += dot(cell, cell.yzx + 33.33);
    return fract((cell.x + cell.y) * cell.z);
  }

  float fabricNoise(vec3 samplePoint, vec3 pixelFootprint) {
    float detail = 1.0 - smoothstep(0.28, 1.05, length(pixelFootprint));
    if (detail <= 0.0) return 0.0;
    vec3 cell = floor(samplePoint);
    vec3 f = fract(samplePoint);
    f = f * f * (3.0 - 2.0 * f);
    float bottom = mix(
      mix(fabricCell(cell), fabricCell(cell + vec3(1, 0, 0)), f.x),
      mix(fabricCell(cell + vec3(0, 1, 0)), fabricCell(cell + vec3(1, 1, 0)), f.x), f.y);
    float top = mix(
      mix(fabricCell(cell + vec3(0, 0, 1)), fabricCell(cell + vec3(1, 0, 1)), f.x),
      mix(fabricCell(cell + vec3(0, 1, 1)), fabricCell(cell + vec3(1, 1, 1)), f.x), f.y);
    return (mix(bottom, top, f.z) - 0.5) * detail;
  }

  float fabricRelief(vec3 p, vec3 pixel) {
    vec3 nap = vec3(0.63, 0.34, 0.55);
    vec3 fuzz = vec3(3.1, 4.0, 3.5);
    return 0.085 * fabricNoise(p * 0.073, pixel * 0.073) +
      0.185 * fabricNoise(p * nap + vec3(7.1, 2.8, 5.6), pixel * nap) +
      0.11 * fabricNoise(p * fuzz + vec3(3.2, 6.5, 1.4), pixel * fuzz);
  }
`;

// Model coordinates are in millimetres. A shared 3D field avoids projection seams on rounded faces.
export function withFabricWeave(material, masked = false) {
  const mask = masked ? "varying float vFabricMask;" : "";
  const onFabric = code => masked ? `if (vFabricMask > 0.5) { ${code} }` : code;
  material.customProgramCacheKey = () => masked ? "fabric-weave-masked" : "fabric-weave";
  material.onBeforeCompile = shader => {
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>", "#include <common>\n" + position + "\n" + mask +
        (masked ? "\nattribute float fabricMask;\n" : "\n"))
      .replace("#include <begin_vertex>", `#include <begin_vertex>
        vFabricPosition = position;
        ${masked ? "vFabricMask = fabricMask;" : ""}
      `);
    const shade = `
      float fabricVariation = fabricRelief(vFabricPosition, fabricPixel);
      diffuseColor.rgb *= 1.0 + fabricVariation;
      roughnessFactor = clamp(0.94 - 0.20 * fabricVariation, 0.88, 1.0);
    `;
    const sheen = `
      float grazing = 1.0 - clamp(dot(normal, normalize(vViewPosition)), 0.0, 1.0);
      outgoingLight += 0.085 * grazing * grazing * totalDiffuse;
    `;
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>", `#include <common>\n${position}\n${mask}\n${microfiber}`)
      .replace("#include <roughnessmap_fragment>", `#include <roughnessmap_fragment>
        vec3 fabricPixel = fwidth(vFabricPosition);
        ${onFabric(shade)}
      `)
      .replace("#include <opaque_fragment>", `${onFabric(sheen)}\n#include <opaque_fragment>`);
  };
  return material;
}
