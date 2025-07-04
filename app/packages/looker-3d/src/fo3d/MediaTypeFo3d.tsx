import { LoadingDots } from "@fiftyone/components";
import { usePluginSettings } from "@fiftyone/plugins";
import * as fos from "@fiftyone/state";
import { useBrowserStorage } from "@fiftyone/state";
import {
  AdaptiveDpr,
  AdaptiveEvents,
  Bvh,
  CameraControls,
  OrbitControls,
  PerspectiveCamera as PerspectiveCameraDrei,
} from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import CameraControlsImpl from "camera-controls";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRecoilCallback, useRecoilValue } from "recoil";
import * as THREE from "three";
import { Vector3 } from "three";
import { CAMERA_POSITION_KEY } from "../Environment";
import { SpinningCube } from "../SpinningCube";
import { StatusBar, StatusTunnel } from "../StatusBar";
import {
  DEFAULT_CAMERA_POSITION,
  RAY_CASTING_SENSITIVITY,
  SET_EGO_VIEW_EVENT,
  SET_TOP_VIEW_EVENT,
} from "../constants";
import { StatusBarRootContainer } from "../containers";
import { useFo3d, useHotkey, useTrackStatus } from "../hooks";
import { useFo3dBounds } from "../hooks/use-bounds";
import { ThreeDLabels } from "../labels";
import type { Looker3dSettings } from "../settings";
import {
  activeNodeAtom,
  cameraPositionAtom,
  isFo3dBackgroundOnAtom,
} from "../state";
import { FoSceneComponent } from "./FoScene";
import { Gizmos } from "./Gizmos";
import HoverMetadataHUD from "./HoverMetadataHUD";
import { Fo3dSceneContext } from "./context";
import { SceneControls } from "./scene-controls/SceneControls";
import {
  getFo3dRoot,
  getMediaPathForFo3dSample,
  getOrthonormalAxis,
} from "./utils";

const CANVAS_WRAPPER_ID = "sample3d-canvas-wrapper";

export const MediaTypeFo3dComponent = () => {
  const sample = useRecoilValue(fos.fo3dSample);
  const mediaField = useRecoilValue(fos.selectedMediaField(true));

  const settings = usePluginSettings<Looker3dSettings>("3d");

  const mediaPath = useMemo(
    () => getMediaPathForFo3dSample(sample, mediaField),
    [mediaField, sample]
  );

  const mediaUrl = useMemo(() => fos.getSampleSrc(mediaPath), [mediaPath]);

  const fo3dRoot = useMemo(() => getFo3dRoot(sample.sample.filepath), [sample]);

  const { foScene, isLoading: isParsingFo3d } = useFo3d(
    mediaUrl,
    sample.sample.filepath,
    fo3dRoot
  );

  const [isSceneInitialized, setSceneInitialized] = useState(false);

  useHotkey(
    "KeyB",
    ({ set }) => {
      set(isFo3dBackgroundOnAtom, (prev) => !prev);
    },
    []
  );

  const [upVector, setUpVectorVal] = fos.useBrowserStorage<Vector3>(
    "fo3d-up-vector",
    () => {
      if (foScene?.cameraProps.up) {
        const mayBeUp = foScene.cameraProps.up;
        if (mayBeUp === "X") {
          return new Vector3(1, 0, 0);
        }
        if (mayBeUp === "Y") {
          return new Vector3(0, 1, 0);
        }
        if (mayBeUp === "Z") {
          return new Vector3(0, 0, 1);
        }
      }

      if (settings.defaultUp) {
        const maybeOrthonormalAxis = getOrthonormalAxis(settings.defaultUp);

        if (maybeOrthonormalAxis) {
          return new Vector3(
            settings.defaultUp[0],
            settings.defaultUp[1],
            settings.defaultUp[2]
          );
        }
      }

      // default to y-up
      return new Vector3(0, 1, 0);
    },
    false,
    {
      parse: (upVectorStr) => {
        try {
          const [x, y, z] = JSON.parse(upVectorStr);
          return new Vector3(x, y, z);
        } catch (error) {
          return new Vector3(0, 1, 0);
        }
      },
      stringify: (upVector) => JSON.stringify(upVector.toArray()),
    }
  );

  const cameraRef = useRef<THREE.PerspectiveCamera>();
  const cameraControlsRef = useRef<CameraControls>();

  const keyState = useRef({
    shiftRight: false,
    shiftLeft: false,
    controlRight: false,
    controlLeft: false,
  });

  const updateCameraControlsConfig = useCallback(() => {
    if (keyState.current.shiftRight || keyState.current.shiftLeft) {
      cameraControlsRef.current.mouseButtons.left =
        CameraControlsImpl.ACTION.TRUCK;
    } else if (keyState.current.controlRight || keyState.current.controlLeft) {
      cameraControlsRef.current.mouseButtons.left =
        CameraControlsImpl.ACTION.DOLLY;
    } else {
      cameraControlsRef.current.mouseButtons.left =
        CameraControlsImpl.ACTION.ROTATE;
    }
  }, [keyState]);

  fos.useEventHandler(document, "keydown", (e: KeyboardEvent) => {
    if (e.code === "ShiftRight") keyState.current.shiftRight = true;
    if (e.code === "ShiftLeft") keyState.current.shiftLeft = true;
    if (e.code === "ControlRight") keyState.current.controlRight = true;
    if (e.code === "ControlLeft") keyState.current.controlLeft = true;
    updateCameraControlsConfig();
  });

  fos.useEventHandler(document, "keyup", (e: KeyboardEvent) => {
    if (e.code === "ShiftRight") keyState.current.shiftRight = false;
    if (e.code === "ShiftLeft") keyState.current.shiftLeft = false;
    if (e.code === "ControlRight") keyState.current.controlRight = false;
    if (e.code === "ControlLeft") keyState.current.controlLeft = false;
    updateCameraControlsConfig();
  });

  const assetsGroupRef = useRef<THREE.Group>();
  const sceneBoundingBox = useFo3dBounds(assetsGroupRef);

  const topCameraPosition = useMemo(() => {
    if (
      !sceneBoundingBox ||
      Math.abs(sceneBoundingBox.max.x) === Number.POSITIVE_INFINITY
    ) {
      return DEFAULT_CAMERA_POSITION();
    }

    const center = sceneBoundingBox.getCenter(new Vector3());
    const size = sceneBoundingBox.getSize(new Vector3());
    if (upVector.y === 1) {
      return new Vector3(
        center.x,
        center.y + Math.max(size.y, size.x, size.z) * 2.5,
        0
      );
    } else if (upVector.x === 1) {
      return new Vector3(
        center.x + Math.max(size.x, size.z, size.y) * 2.5,
        center.y,
        0
      );
    } else {
      // assume z-up
      return new Vector3(
        center.x,
        0,
        center.z + Math.max(size.z, size.x, size.y) * 2.5
      );
    }
  }, [sceneBoundingBox, upVector]);

  const overridenCameraPosition = useRecoilValue(cameraPositionAtom);

  const lastSavedCameraPosition = useMemo(() => {
    const lastSavedCameraPosition =
      window?.localStorage.getItem(CAMERA_POSITION_KEY);

    return lastSavedCameraPosition ? JSON.parse(lastSavedCameraPosition) : null;
  }, []);

  const getDefaultCameraPosition = useCallback(
    (ignoreLastSavedCameraPosition = false) => {
      /**
       * This is the order of precedence for the camera position:
       * 1. If the user has set a camera position via operator by writing to `cameraPositionAtom`, use that
       * 2. If the user has set a default camera position in the scene itself, use that
       * 3. If the user has set a default camera position in the plugin settings, use that
       * 4. If the user has set a default camera position in the browser storage, use that
       * 5. Compute a default camera position based on the bounding box of the scene
       * 6. Use an arbitrary default camera position
       */

      if (isParsingFo3d) {
        return DEFAULT_CAMERA_POSITION();
      }

      if (overridenCameraPosition?.length === 3) {
        return new Vector3(
          overridenCameraPosition[0],
          overridenCameraPosition[1],
          overridenCameraPosition[2]
        );
      }

      if (
        !ignoreLastSavedCameraPosition &&
        lastSavedCameraPosition &&
        lastSavedCameraPosition.length === 3
      ) {
        return new Vector3(
          lastSavedCameraPosition[0],
          lastSavedCameraPosition[1],
          lastSavedCameraPosition[2]
        );
      }

      const defaultCameraPosition = foScene?.cameraProps.position;

      if (defaultCameraPosition) {
        return new Vector3(
          defaultCameraPosition[0],
          defaultCameraPosition[1],
          defaultCameraPosition[2]
        );
      }

      if (settings.defaultCameraPosition) {
        return new Vector3(
          settings.defaultCameraPosition.x,
          settings.defaultCameraPosition.y,
          settings.defaultCameraPosition.z
        );
      }

      if (
        sceneBoundingBox &&
        Math.abs(sceneBoundingBox.max.x) !== Number.POSITIVE_INFINITY
      ) {
        const center = sceneBoundingBox.getCenter(new Vector3());
        const size = sceneBoundingBox.getSize(new Vector3());

        if (upVector.y === 1) {
          return new Vector3(
            center.x,
            center.y + Math.max(size.y / 2, 1.5),
            center.z + Math.max(size.x, size.y, size.z) * 2
          );
        } else if (upVector.x === 1) {
          return new Vector3(
            center.x + Math.max(size.x / 2, 1.5),
            center.y + Math.max(size.x, size.y, size.z) * 2,
            center.z
          );
        } else {
          // assume z-up
          return new Vector3(
            center.x,
            center.y - Math.max(size.x, size.y, size.z) * 2,
            center.z + Math.max(1.5, size.z / 2)
          );
        }
      }

      return DEFAULT_CAMERA_POSITION();
    },
    [
      settings,
      overridenCameraPosition,
      isParsingFo3d,
      foScene,
      sceneBoundingBox,
      upVector,
      lastSavedCameraPosition,
    ]
  );

  const defaultCameraPositionComputed = useMemo(
    () => getDefaultCameraPosition(),
    [getDefaultCameraPosition]
  );

  const resetActiveNode = useRecoilCallback(
    ({ set }) =>
      () => {
        set(activeNodeAtom, null);
        setHoverMetadata(null);
        setAutoRotate(false);
      },
    []
  );

  useLayoutEffect(() => {
    const canvas = document.getElementById(CANVAS_WRAPPER_ID);

    if (canvas) {
      canvas.querySelector("canvas")?.setAttribute("canvas-loaded", "true");
    }
  }, [isSceneInitialized]);

  useEffect(() => {
    resetActiveNode();
  }, [isSceneInitialized, resetActiveNode]);

  const onChangeView = useCallback(
    (
      view: "pov" | "top",
      {
        useAnimation = true,
        ignoreLastSavedCameraPosition = false,
        isFirstTime = false,
      }: {
        useAnimation?: boolean;
        ignoreLastSavedCameraPosition?: boolean;
        isFirstTime?: boolean;
      } = {}
    ) => {
      if (
        !sceneBoundingBox ||
        !cameraRef.current ||
        !cameraControlsRef.current
      ) {
        return;
      }

      const defaultCameraPosition = getDefaultCameraPosition(
        ignoreLastSavedCameraPosition
      );

      let newCameraPosition = [
        defaultCameraPosition.x,
        defaultCameraPosition.y,
        defaultCameraPosition.z,
      ] as const;

      if (view === "top") {
        newCameraPosition = [
          topCameraPosition.x,
          topCameraPosition.y,
          topCameraPosition.z,
        ];
      }

      const boundingBoxCenter = sceneBoundingBox.getCenter(new Vector3());
      const newLookAt = [
        boundingBoxCenter.x,
        boundingBoxCenter.y,
        boundingBoxCenter.z,
      ] as const;

      cameraControlsRef.current.setLookAt(
        ...newCameraPosition,
        ...newLookAt,
        useAnimation
      );

      if (isFirstTime) {
        setSceneInitialized(true);
      }
    },
    [
      sceneBoundingBox,
      topCameraPosition,
      getDefaultCameraPosition,
      setSceneInitialized,
    ]
  );

  fos.useEventHandler(window, SET_TOP_VIEW_EVENT, () => {
    onChangeView("top", {
      useAnimation: true,
      ignoreLastSavedCameraPosition: true,
    });
  });

  fos.useEventHandler(window, SET_EGO_VIEW_EVENT, () => {
    onChangeView("pov", {
      useAnimation: true,
      ignoreLastSavedCameraPosition: true,
    });
  });

  useHotkey(
    "KeyT",
    ({}) => {
      onChangeView("top", {
        useAnimation: true,
        ignoreLastSavedCameraPosition: true,
      });
    },
    [onChangeView]
  );

  useHotkey(
    "KeyE",
    ({}) => {
      onChangeView("pov", {
        useAnimation: true,
        ignoreLastSavedCameraPosition: true,
      });
    },
    [onChangeView]
  );

  // zoom to selected labels and use them as the new lookAt
  useHotkey(
    "KeyZ",
    async ({ snapshot }) => {
      const currentSelectedLabels = await snapshot.getPromise(
        fos.selectedLabels
      );

      if (currentSelectedLabels.length === 0) {
        return;
      }

      const labelBoundingBoxes: THREE.Box3[] = [];

      for (const selectedLabel of currentSelectedLabels) {
        const field = selectedLabel.field;
        const labelId = selectedLabel.labelId;

        const labelFieldData = sample.sample[field];

        let thisLabel = null;

        if (Array.isArray(labelFieldData)) {
          // if the field data is an array of labels
          thisLabel = labelFieldData.find(
            (l) => l._id === labelId || l.id === labelId
          );
        } else if (
          labelFieldData &&
          labelFieldData.detections &&
          Array.isArray(labelFieldData.detections)
        ) {
          // if the field data contains detections
          thisLabel = labelFieldData.detections.find(
            (l) => l._id === labelId || l.id === labelId
          );
        } else {
          // single label
          thisLabel = labelFieldData;
        }

        if (!thisLabel) {
          continue;
        }

        const thisLabelDimension = thisLabel.dimensions as [
          number,
          number,
          number
        ];
        const thisLabelLocation = thisLabel.location as [
          number,
          number,
          number
        ];

        const thisLabelBoundingBox = new THREE.Box3();
        thisLabelBoundingBox.setFromCenterAndSize(
          new THREE.Vector3(...thisLabelLocation),
          new THREE.Vector3(...thisLabelDimension)
        );

        labelBoundingBoxes.push(thisLabelBoundingBox);
      }

      const unionBoundingBox: THREE.Box3 = labelBoundingBoxes[0].clone();

      for (let i = 1; i < labelBoundingBoxes.length; i++) {
        unionBoundingBox.union(labelBoundingBoxes[i]);
      }

      // center = (min + max) / 2
      let unionBoundingBoxCenter = new Vector3();
      unionBoundingBoxCenter = unionBoundingBoxCenter
        .addVectors(unionBoundingBox.min, unionBoundingBox.max)
        .multiplyScalar(0.5);

      // size = max - min
      let unionBoundingBoxSize = new Vector3();
      unionBoundingBoxSize = unionBoundingBoxSize.subVectors(
        unionBoundingBox.max,
        unionBoundingBox.min
      );

      const maxSize = Math.max(
        unionBoundingBoxSize.x,
        unionBoundingBoxSize.y,
        unionBoundingBoxSize.z
      );

      const newCameraPosition = new THREE.Vector3();

      if (upVector.y === 1) {
        newCameraPosition.set(
          unionBoundingBoxCenter.x,
          // times 3 (arbitrary) to make sure the camera is not inside the bounding box
          unionBoundingBoxCenter.y + maxSize * 3,
          unionBoundingBoxCenter.z
        );
      } else if (upVector.x === 1) {
        newCameraPosition.set(
          // times 3 (arbitrary) to make sure the camera is not inside the bounding box
          unionBoundingBoxCenter.x + maxSize * 2,
          unionBoundingBoxCenter.y,
          unionBoundingBoxCenter.z
        );
      } else {
        // assume z-up
        newCameraPosition.set(
          unionBoundingBoxCenter.x,
          unionBoundingBoxCenter.y,
          // times 3 (arbitrary) to make sure the camera is not inside the bounding box
          unionBoundingBoxCenter.z + maxSize * 2
        );
      }

      await cameraControlsRef.current.setLookAt(
        newCameraPosition.x,
        newCameraPosition.y,
        newCameraPosition.z,
        unionBoundingBoxCenter.x,
        unionBoundingBoxCenter.y,
        unionBoundingBoxCenter.z,
        true
      );
    },
    [sample, upVector],
    {
      useTransaction: false,
    }
  );

  // this effect runs after the scene is initialized
  // and sets the appropriate lookAt and camera position
  useEffect(() => {
    if (!cameraControlsRef.current || !cameraRef.current) {
      return;
    }

    if (foScene?.cameraProps.lookAt?.length === 3) {
      cameraControlsRef.current.setTarget(
        foScene.cameraProps.lookAt[0],
        foScene.cameraProps.lookAt[1],
        foScene.cameraProps.lookAt[2],
        false
      );
      return;
    } else {
      onChangeView("pov", {
        useAnimation: false,
        ignoreLastSavedCameraPosition: false,
        isFirstTime: true,
      });
    }
  }, [foScene, onChangeView, cameraControlsRef, cameraRef]);

  useTrackStatus();

  const setUpVector = useCallback((upVector: Vector3) => {
    setUpVectorVal(upVector);
  }, []);

  const [autoRotate, setAutoRotate] = useBrowserStorage(
    "fo3dAutoRotate",
    false
  );

  const [pointCloudSettings, setPointCloudSettings] = useBrowserStorage(
    "fo3dPointCloudSettings",
    {
      enableTooltip: false,
      rayCastingSensitivity: "high",
    }
  );

  const [hoverMetadata, setHoverMetadata] = useState<Record<
    string,
    unknown
  > | null>(null);

  if (isParsingFo3d) {
    return <LoadingDots />;
  }

  return (
    <>
      <HoverMetadataHUD hoverMetadata={hoverMetadata} />
      <Canvas
        id={CANVAS_WRAPPER_ID}
        onPointerMissed={resetActiveNode}
        key={upVector.toArray().join(",")}
        raycaster={{
          params: {
            Points: {
              threshold:
                RAY_CASTING_SENSITIVITY[
                  pointCloudSettings.rayCastingSensitivity
                ],
            },
          },
        }}
      >
        <StatusTunnel.Out />
        <Fo3dSceneContext.Provider
          value={{
            isSceneInitialized,
            upVector,
            setUpVector,
            fo3dRoot,
            sceneBoundingBox,
            autoRotate,
            setAutoRotate,
            pointCloudSettings,
            setPointCloudSettings,
            hoverMetadata,
            setHoverMetadata,
            pluginSettings: settings,
          }}
        >
          <PerspectiveCameraDrei
            makeDefault
            ref={cameraRef}
            position={defaultCameraPositionComputed}
            up={upVector}
            fov={foScene?.cameraProps.fov ?? 50}
            near={foScene?.cameraProps.near ?? 0.1}
            far={foScene?.cameraProps.far ?? 2500}
            aspect={foScene?.cameraProps.aspect ?? 1}
            onUpdate={(cam) => cam.updateProjectionMatrix()}
          />
          <AdaptiveDpr pixelated />
          <AdaptiveEvents />
          {!autoRotate && <CameraControls ref={cameraControlsRef} />}
          {autoRotate && <OrbitControls autoRotate={autoRotate} makeDefault />}
          <SceneControls scene={foScene} />
          <Gizmos />

          {!isSceneInitialized && <SpinningCube />}

          <Bvh firstHitOnly enabled={pointCloudSettings.enableTooltip}>
            <group ref={assetsGroupRef} visible={isSceneInitialized}>
              <FoSceneComponent scene={foScene} />
            </group>
          </Bvh>

          {isSceneInitialized && <ThreeDLabels sampleMap={{ fo3d: sample }} />}
        </Fo3dSceneContext.Provider>
      </Canvas>
      <StatusBarRootContainer>
        <StatusBar cameraRef={cameraRef} />
      </StatusBarRootContainer>
    </>
  );
};
