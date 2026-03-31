// Type augmentation for leaflet.heat — adds L.heatLayer to the leaflet module.
// leaflet.heat has no official @types package; this minimal declaration covers our usage.

import "leaflet";

declare module "leaflet" {
  function heatLayer(
    latlngs: Array<[number, number] | [number, number, number]>,
    options?: {
      minOpacity?: number;
      maxZoom?: number;
      max?: number;
      radius?: number;
      blur?: number;
      gradient?: Record<number, string>;
    }
  ): Layer;
}

declare module "leaflet.heat" {
  // Side-effect only import — augments the global L object with heatLayer.
}
